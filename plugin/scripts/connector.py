from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from xml.sax.saxutils import escape

import configure
import localexec
# runuser can preserve an inaccessible /root cwd; FastMCP reads .env during import.
def _ensure_accessible_cwd():
    try:
        os.stat(".")
        if os.access(".", os.R_OK | os.X_OK):
            return
    except OSError:
        pass
    os.chdir(str(Path.home()))

_ensure_accessible_cwd()
import relay_agent
from configmodel import normalize, normalize_workspace_path, path_style, to_storage
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


def write_remote_config(
    host: str,
    root: str,
    project: str = "workspace",
    *,
    local: bool = False,
) -> Path:
    root = normalize_workspace_path(root)
    style = path_style(root)
    parsed_root = PureWindowsPath(root) if style == "windows" else PurePosixPath(root)
    if not parsed_root.is_absolute():
        raise ValueError("root must be an absolute workspace path")
    transport = "local" if local else "ssh"
    host_token = "local" if local else configure.validate_host(host)
    payload = {
        "default_host": "main",
        "hosts": {
            "main": {
                "transport": transport,
                "ssh_host": host_token,
                "path_style": style,
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
    payload = to_storage(normalize(payload))
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
        # Replacing the inode also works while the old Linux binary is running.
        temporary = destination.with_suffix(destination.suffix + ".next")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    if os.name != "nt":
        destination.chmod(destination.stat().st_mode | 0o111)
    return [str(destination)]


def _run(argv: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=check,
        env=env,
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


def ensure_linux_linger() -> None:
    user = getpass.getuser()
    check = _run(
        ["loginctl", "show-user", user, "-p", "Linger", "--value"],
        check=False,
    )
    if check.returncode == 0 and check.stdout.strip().lower() == "yes":
        return
    enabled = _run(["loginctl", "enable-linger", user], check=False)
    if enabled.returncode == 0:
        verify = _run(
            ["loginctl", "show-user", user, "-p", "Linger", "--value"],
            check=False,
        )
        if verify.returncode == 0 and verify.stdout.strip().lower() == "yes":
            return
    detail = (
        enabled.stderr.strip()
        or enabled.stdout.strip()
        or check.stderr.strip()
        or "loginctl did not enable linger"
    )
    raise RuntimeError(
        "persistent Linux Connector requires systemd linger for "
        f"{user}; run 'sudo loginctl enable-linger {user}' and rerun install: {detail}"
    )


def _user_systemd_environment() -> dict[str, str]:
    env = dict(os.environ)
    uid = os.getuid()
    runtime = Path(f"/run/user/{uid}")
    if runtime.is_dir() and runtime.stat().st_uid == uid:
        env["XDG_RUNTIME_DIR"] = str(runtime)
        if (runtime / "bus").exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return env


def install_linux_user_service(command: list[str]) -> None:
    ensure_linux_linger()
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
    environment = _user_systemd_environment()
    reload = _run(["systemctl", "--user", "daemon-reload"], check=False, env=environment)
    if reload.returncode != 0:
        raise RuntimeError("Unable to access systemd user manager; check linger and user@UID.service: " + (reload.stderr or reload.stdout).strip())
    was_active = _run(["systemctl", "--user", "is-active", "--quiet", LINUX_SERVICE], check=False, env=environment).returncode == 0
    enabled = _run(["systemctl", "--user", "enable", "--now", LINUX_SERVICE], check=False, env=environment)
    if enabled.returncode != 0:
        raise RuntimeError(
            (enabled.stderr or enabled.stdout or "systemd user service failed")
            + "\nRun the connector manually with: "
            + _systemd_exec(command)
        )
    if was_active:
        # The installer can be a child of the Connector being upgraded. Queue the
        # restart in a separate systemd unit so the tool can deliver its receipt
        # before the old service cgroup is stopped.
        restarted = _run([
            "systemd-run", "--user", "--quiet", "--collect", "--on-active=3s",
            "systemctl", "--user", "restart", LINUX_SERVICE,
        ], check=False, env=environment)
        if restarted.returncode != 0:
            raise RuntimeError("Unable to activate installed Connector: " + (restarted.stderr or restarted.stdout).strip())


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


def read_pair_file(path: Path) -> str:
    """Read a one-time pairing code without exposing it in process argv."""
    import stat
    resolved = path.expanduser().resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("pairing file must be an owner-controlled regular file")
    if info.st_mode & 0o077:
        raise ValueError("pairing file must not be accessible by group or others")
    code = resolved.read_text(encoding="utf-8").strip()
    if len(code) != 9 or code[4] != "-" or not code.replace("-", "").isalnum():
        raise ValueError("invalid pairing code format")
    return code


def install(
    *,
    pair_code: str | None,
    host: str,
    root: str,
    relay_url: str,
    name: str,
    project: str,
    skip_ssh_test: bool = False,
    local: bool = False,
) -> dict[str, Any]:
    if not local and not skip_ssh_test:
        configure.test_ssh(host)
    remote_path = write_remote_config(host, root, project, local=local)
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
        "transport": "local" if local else "ssh",
    }


def configure_codex(executable: str, project: str = "workspace") -> dict[str, Any]:
    """Explicit, local-only registration of an already installed native CLI."""
    import adapterctl
    import agent_policy

    binary = Path(executable).expanduser().resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Codex executable must be an existing executable file")
    config = json.loads(remote_config_path().read_text(encoding="utf-8"))
    projects = config.get("projects", {})
    target = projects.get(project) if isinstance(projects, dict) else None
    if not isinstance(target, dict) or target.get("host") != "main":
        raise ValueError("Codex project must already be configured on the local Connector")
    hosts = config.get("hosts", {})
    host = hosts.get("main") if isinstance(hosts, dict) else None
    if not isinstance(host, dict) or host.get("transport") != "local":
        raise ValueError("Codex must execute on this Connector's local host")
    cwd = Path(target["path"]).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("Codex project workspace must exist")

    policy = agent_policy.load()
    workspace_map = dict(policy.get("claude_workspaces", {}))
    workspace_map[f"main:{project}"] = str(cwd)
    policy["claude_workspaces"] = workspace_map
    allowlist = list(policy.get("launch_allowlist", []))
    launch_key = f"main:{project}:codex"
    if launch_key not in allowlist:
        allowlist.append(launch_key)
    policy["launch_allowlist"] = allowlist
    policy["codex_executable"] = str(binary)
    adapterctl.save(agent_policy.policy_path(), policy)
    manifest_path = adapterctl.location()
    manifest = adapterctl.load(manifest_path)
    entry = {"module": "livingruntime_adapter_codex", "factory": "make"}
    if not any(row.get("module") == entry["module"] for row in manifest["adapters"]):
        manifest["adapters"].append(entry)
        adapterctl.save(manifest_path, manifest)

    # Reload the already-paired process AFTER this command exits. No re-pairing.
    restart = None
    if sys.platform.startswith("linux"):
        restart = _run(
            ["systemd-run", "--user", "--quiet", "--collect", "--on-active=3s",
             "systemctl", "--user", "restart", LINUX_SERVICE], check=False
        )
    return {"configured": True, "adapter": "codex", "project": project,
            "cli": str(binary), "workspace": str(cwd),
            "restart_scheduled": bool(restart and restart.returncode == 0)}


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
            cfg = normalize(payload)
            host = cfg["hosts"][cfg["default_host"]]
            if host["transport"] == "local":
                result["transport"] = "local"
                result["local"] = "ok"
            else:
                configure.test_ssh(host["ssh_host"])
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
    if os.name == "nt":
        print("This connects ChatGPT directly to a bounded workspace on this Windows computer.")
        print("Native Windows mode does not require localhost SSH.")
    else:
        print("This connects ChatGPT to a Linux host you can already access with SSH keys.")
        print("SSH credentials stay in your normal SSH configuration and are never uploaded.")
    print()
    pair_code = input("Pairing code from ChatGPT (XXXX-XXXX): ").strip()
    if os.name == "nt":
        host = "local"
        root = input(r"Allowed Windows workspace root (for example D:\Projects): ").strip()
        local = True
    else:
        # The common path is to pair this machine, not configure SSH into a
        # second host. Advanced SSH routing remains available on request.
        mode = input("Connect this machine directly? [Y/n]: ").strip().lower()
        local = mode not in {"n", "no"}
        host = "local" if local else input("SSH host or user@host: ").strip()
        default_root = str(Path.home() / "workspace")
        root = input(f"Allowed workspace root [{default_root}]: ").strip() or default_root
        if local and root == default_root:
            Path(root).mkdir(parents=True, exist_ok=True)
    result = install(
        pair_code=pair_code,
        host=host,
        root=root,
        relay_url=DEFAULT_RELAY_URL,
        name=socket.gethostname(),
        project="workspace",
        local=local,
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
    pair_source = install_parser.add_mutually_exclusive_group()
    pair_source.add_argument("--pair", dest="pair_code", help="One-time pairing code from ChatGPT")
    pair_source.add_argument("--pair-file", type=Path, help="Owner-only file containing one-time pairing code")
    install_parser.add_argument("--host", help="SSH alias or user@host")
    install_parser.add_argument("--root", required=True, help="Allowed workspace root")
    install_parser.add_argument(
        "--local",
        action="store_true",
        help="Use the Connector machine itself as the target instead of SSH",
    )
    install_parser.add_argument("--relay", default=DEFAULT_RELAY_URL)
    install_parser.add_argument("--name", default=socket.gethostname())
    install_parser.add_argument("--project", default="workspace")
    install_parser.add_argument("--skip-ssh-test", action="store_true")

    run_parser = sub.add_parser("run", help="Run the connector in the foreground")
    run_parser.add_argument("--config", type=Path, default=relay_config_path())
    run_parser.add_argument("--daemon-log", action="store_true", help=argparse.SUPPRESS)

    status_parser = sub.add_parser("status", help="Show connector configuration status")
    status_parser.add_argument("--check-ssh", action="store_true")

    codex_parser = sub.add_parser("configure-codex", help="Enable an installed Codex CLI on this local Connector")
    codex_parser.add_argument("--executable", required=True, help="Absolute path to existing Codex CLI")
    codex_parser.add_argument("--project", default="workspace")

    uninstall_parser = sub.add_parser("uninstall", help="Remove connector autostart")
    uninstall_parser.add_argument("--purge", action="store_true", help="Also delete local connector configs")

    worker_parser = sub.add_parser("local-worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("receipt_path")
    worker_parser.add_argument("spec_path")

    agent_worker_parser = sub.add_parser("agent-worker", help=argparse.SUPPRESS)
    agent_worker_parser.add_argument("adapter", choices=("claude-code", "codex"))
    agent_worker_parser.add_argument("record")

    args = parser.parse_args(actual_argv)
    if args.command == "install":
        if not args.local and not args.host:
            parser.error("--host is required unless --local is used")
        result = install(
            pair_code=read_pair_file(args.pair_file) if args.pair_file else args.pair_code,
            host=args.host or "local",
            root=args.root,
            relay_url=args.relay,
            name=args.name,
            project=args.project,
            skip_ssh_test=args.skip_ssh_test,
            local=args.local,
        )
        print(json.dumps(result, indent=2))
        print("LivingRuntime Remote is connected. Return to ChatGPT and call connection_status.")
        return
    if args.command == "run":
        run_connector(args.config, daemon_log=args.daemon_log)
        return
    if args.command == "configure-codex":
        print(json.dumps(configure_codex(args.executable, args.project), indent=2))
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
    if args.command == "agent-worker":
        # Private entry point for PyInstaller one-file bundles: their embedded
        # Python modules are not accessible as disk scripts to a subprocess.
        # Only accept owner-scoped, pre-existing native session records.
        import uuid
        kind = "codex" if args.adapter == "codex" else "claude"
        root = Path(os.environ.get(
            f"LIVINGRUNTIME_{kind.upper()}_JOBS",
            str(Path.home() / ".livingruntime" / f"{kind}-jobs"),
        )).expanduser().resolve()
        record = Path(args.record).expanduser().resolve(strict=True)
        try:
            parsed = uuid.UUID(record.stem)
        except ValueError:
            raise ValueError("invalid agent-worker session path") from None
        if (record.parent != root or record.suffix != ".json"
                or str(parsed) != record.stem or record.stat().st_uid != os.getuid()):
            raise PermissionError("agent-worker record must belong to this user and session root")
        if args.adapter == "codex":
            import codex_worker
            codex_worker.main(str(record))
        else:
            import claude_worker
            claude_worker.main(str(record))
        return
    if args.command == "local-worker":
        raise SystemExit(localexec.detached_worker(args.receipt_path, args.spec_path))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, PermissionError) as exc:
        print(f"Connector setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
