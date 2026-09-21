from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import configure
import relay_agent
from contract import PLUGIN_VERSION

PRODUCT = "LivingRuntime Remote"
DEFAULT_RELAY_URL = os.environ.get(
    "LIVINGRUNTIME_RELAY_URL", "https://remote.livingruntime.com"
).rstrip("/")
WINDOWS_TASK = "LivingRuntimeRemoteConnector"
LINUX_SERVICE = "livingruntime-remote-connector.service"
MACOS_LABEL = "com.livingruntime.remote.connector"


def state_dir() -> Path:
    path = Path.home() / ".livingruntime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def relay_config_path() -> Path:
    return state_dir() / "relay.json"


def remote_config_path() -> Path:
    return state_dir() / "remote.json"


def connector_log_path() -> Path:
    return state_dir() / "connector.log"


def bin_dir() -> Path:
    path = state_dir() / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_remote_config(host: str, root: str, project: str = "workspace") -> Path:
    host = configure.validate_host(host)
    root = str(root or "").strip().replace("\\", "/")
    if not root.startswith("/"):
        raise ValueError("root must be an absolute POSIX path on the SSH target")
    payload = {
        "default_host": "main",
        "hosts": {
            "main": {
                "ssh_host": host,
                "roots": [root],
                "units": [],
            }
        },
        "projects": {
            project: {
                "host": "main",
                "path": root,
            }
        },
    }
    path = remote_config_path()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _chmod_private(path)
    return path


def current_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [sys.executable, str(Path(__file__).resolve())]


def install_binary() -> list[str]:
    command = current_command()
    if not getattr(sys, "frozen", False):
        return command
    source = Path(sys.executable).resolve()
    suffix = ".exe" if os.name == "nt" else ""
    destination = bin_dir() / f"livingruntime-remote-connector{suffix}"
    if not destination.exists() or source != destination.resolve():
        shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(destination.stat().st_mode | 0o111)
    return [str(destination)]


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _windows_task_command(command: list[str]) -> str:
    return subprocess.list2cmdline([*command, "run", "--daemon-log"])


def install_windows_task(command: list[str]) -> None:
    task_command = _windows_task_command(command)
    _run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"], check=False)
    created = _run(
        [
            "schtasks", "/Create", "/TN", WINDOWS_TASK,
            "/SC", "ONLOGON", "/RL", "LIMITED",
            "/TR", task_command, "/F",
        ],
        check=False,
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr or created.stdout or "failed to create Windows task")
    started = _run(["schtasks", "/Run", "/TN", WINDOWS_TASK], check=False)
    if started.returncode != 0:
        raise RuntimeError(started.stderr or started.stdout or "failed to start Windows task")


def _systemd_exec(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in [*command, "run", "--daemon-log"])


def install_linux_user_service(command: list[str]) -> None:
    directory = Path.home() / ".config" / "systemd" / "user"
    directory.mkdir(parents=True, exist_ok=True)
    unit = directory / LINUX_SERVICE
    unit.write_text(
        "[Unit]\n"
        "Description=LivingRuntime Remote Connector\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={_systemd_exec(command)}\n"
        "Restart=always\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    _run(["systemctl", "--user", "daemon-reload"])
    enabled = _run(["systemctl", "--user", "enable", "--now", LINUX_SERVICE], check=False)
    if enabled.returncode != 0:
        raise RuntimeError(
            (enabled.stderr or enabled.stdout or "systemd user service failed")
            + "\nRun the connector manually with: "
            + _systemd_exec(command)
        )


def _launchagent_plist(command: list[str]) -> str:
    args = "\n".join(f"      <string>{escape(part)}</string>" for part in [*command, "run", "--daemon-log"])
    log = escape(str(connector_log_path()))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{MACOS_LABEL}</string>
  <key>ProgramArguments</key><array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def install_macos_launchagent(command: list[str]) -> None:
    directory = Path.home() / "Library" / "LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    plist = directory / f"{MACOS_LABEL}.plist"
    plist.write_text(_launchagent_plist(command), encoding="utf-8")
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{MACOS_LABEL}"], check=False)
    loaded = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=False)
    if loaded.returncode != 0:
        raise RuntimeError(loaded.stderr or loaded.stdout or "failed to load LaunchAgent")


def install_autostart(command: list[str]) -> str:
    if os.name == "nt":
        install_windows_task(command)
        return "windows-task"
    if sys.platform == "darwin":
        install_macos_launchagent(command)
        return "launchagent"
    install_linux_user_service(command)
    return "systemd-user"


def uninstall_autostart() -> None:
    if os.name == "nt":
        _run(["schtasks", "/End", "/TN", WINDOWS_TASK], check=False)
        _run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"], check=False)
        return
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{MACOS_LABEL}"], check=False)
        try:
            plist.unlink()
        except FileNotFoundError:
            pass
        return
    _run(["systemctl", "--user", "disable", "--now", LINUX_SERVICE], check=False)
    unit = Path.home() / ".config" / "systemd" / "user" / LINUX_SERVICE
    try:
        unit.unlink()
    except FileNotFoundError:
        pass
    _run(["systemctl", "--user", "daemon-reload"], check=False)


def install(
    *,
    pair_code: str | None,
    host: str,
    root: str,
    relay_url: str,
    name: str,
    project: str,
    skip_ssh_test: bool = False,
) -> dict[str, Any]:
    if not skip_ssh_test:
        configure.test_ssh(host)
    remote_path = write_remote_config(host, root, project)
    relay_path = relay_config_path()
    paired: dict[str, Any] | None = None
    if pair_code:
        paired = relay_agent.pair(
            relay_agent._check_url(relay_url),
            pair_code.strip().upper(),
            name,
            relay_path,
        )
    elif not relay_path.exists():
        raise RuntimeError("pair code is required for the first installation")
    command = install_binary()
    autostart = install_autostart(command)
    return {
        "ok": True,
        "version": PLUGIN_VERSION,
        "relay": relay_agent._check_url(relay_url),
        "remote_config": str(remote_path),
        "relay_config": str(relay_path),
        "device_id": paired.get("device_id") if paired else None,
        "autostart": autostart,
        "command": command,
    }


def status(check_ssh: bool = False) -> dict[str, Any]:
    relay_path = relay_config_path()
    remote_path = remote_config_path()
    result: dict[str, Any] = {
        "version": PLUGIN_VERSION,
        "platform": platform.system(),
        "paired": relay_path.exists(),
        "configured": remote_path.exists(),
        "relay_config": str(relay_path),
        "remote_config": str(remote_path),
        "log": str(connector_log_path()),
    }
    if relay_path.exists():
        try:
            cfg = relay_agent._load(relay_path)
            result["relay"] = cfg.get("url")
            result["device_id"] = cfg.get("device_id")
        except Exception as exc:
            result["relay_error"] = str(exc)
    if check_ssh and remote_path.exists():
        try:
            payload = json.loads(remote_path.read_text(encoding="utf-8"))
            host = payload["hosts"][payload.get("default_host", "main")]["ssh_host"]
            configure.test_ssh(host)
            result["ssh"] = "ok"
        except Exception as exc:
            result["ssh"] = f"error: {exc}"
    return result


def run_connector(config: Path, daemon_log: bool = False) -> None:
    if daemon_log:
        log = connector_log_path()
        handle = log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle
    relay_agent.serve(config)


def interactive_setup() -> None:
    print(f"{PRODUCT} Connector {PLUGIN_VERSION}")
    print()
    print("This connects ChatGPT to a Linux host you can already access with SSH keys.")
    print("SSH credentials stay in your normal SSH configuration and are never uploaded.")
    print()
    pair_code = input("Pairing code from ChatGPT (XXXX-XXXX): ").strip()
    host = input("SSH host or user@host: ").strip()
    root = input("Allowed workspace root (for example /home/ubuntu): ").strip()
    result = install(
        pair_code=pair_code,
        host=host,
        root=root,
        relay_url=DEFAULT_RELAY_URL,
        name=socket.gethostname(),
        project="workspace",
    )
    print()
    print("Connected successfully.")
    print(f"Autostart: {result['autostart']}")
    print("Return to ChatGPT and run: connection_status")
    if os.name == "nt":
        try:
            input("Press Enter to close...")
        except EOFError:
            pass


def main(argv: list[str] | None = None) -> None:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if not actual_argv:
        interactive_setup()
        return
    parser = argparse.ArgumentParser(
        prog="livingruntime-remote-connector",
        description="Install and run the LivingRuntime Remote Connector.",
    )
    parser.add_argument("--version", action="version", version=PLUGIN_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    install_parser = sub.add_parser("install", help="Pair, configure SSH access, and enable autostart")
    install_parser.add_argument("--pair", dest="pair_code", help="One-time pairing code from ChatGPT")
    install_parser.add_argument("--host", required=True, help="SSH alias or user@host")
    install_parser.add_argument("--root", required=True, help="Allowed workspace root on the SSH target")
    install_parser.add_argument("--relay", default=DEFAULT_RELAY_URL)
    install_parser.add_argument("--name", default=socket.gethostname())
    install_parser.add_argument("--project", default="workspace")
    install_parser.add_argument("--skip-ssh-test", action="store_true")

    run_parser = sub.add_parser("run", help="Run the connector in the foreground")
    run_parser.add_argument("--config", type=Path, default=relay_config_path())
    run_parser.add_argument("--daemon-log", action="store_true", help=argparse.SUPPRESS)

    status_parser = sub.add_parser("status", help="Show connector configuration status")
    status_parser.add_argument("--check-ssh", action="store_true")

    uninstall_parser = sub.add_parser("uninstall", help="Remove connector autostart")
    uninstall_parser.add_argument("--purge", action="store_true", help="Also delete local connector configs")

    args = parser.parse_args(actual_argv)
    if args.command == "install":
        result = install(
            pair_code=args.pair_code,
            host=args.host,
            root=args.root,
            relay_url=args.relay,
            name=args.name,
            project=args.project,
            skip_ssh_test=args.skip_ssh_test,
        )
        print(json.dumps(result, indent=2))
        print("LivingRuntime Remote is connected. Return to ChatGPT and call connection_status.")
        return
    if args.command == "run":
        run_connector(args.config, daemon_log=args.daemon_log)
        return
    if args.command == "status":
        print(json.dumps(status(check_ssh=args.check_ssh), indent=2))
        return
    if args.command == "uninstall":
        uninstall_autostart()
        if args.purge:
            for path in (relay_config_path(), remote_config_path()):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        print("LivingRuntime Remote Connector autostart removed.")
        print("Use disconnect_device in ChatGPT to revoke the paired device.")
        return


if __name__ == "__main__":
    main()
