from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import tunnel
from launcher import ensure_runtime

SERVICE_NAME = "livingruntime-tunnel"
HOST_HEALTH_ADDR = "127.0.0.1:18080"
REMOTE_PLUGIN = Path.home() / ".livingruntime" / "plugin"
SKIP_DIR_NAMES = {"__pycache__", ".git"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def plugin_source() -> Path:
    return Path(__file__).resolve().parents[1]


def ssh_run(host: str, command: str, *, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", host, command],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise SystemExit((completed.stderr or completed.stdout).decode("utf-8", errors="replace") or command)
    return completed


def systemd_unit(*, client: Path, profile: str = tunnel.PROFILE) -> str:
    env_file = Path.home() / ".livingruntime" / "tunnel.env"
    return f"""[Unit]
Description=LivingRuntime Secure MCP Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
EnvironmentFile={env_file.as_posix()}
ExecStart={client.as_posix()} run --profile {profile}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def localize_config(payload: dict) -> dict:
    stored = json.loads(json.dumps(payload))
    hosts = stored.get("hosts")
    if isinstance(hosts, dict):
        for host in hosts.values():
            if isinstance(host, dict):
                host["ssh_host"] = "localhost"
    if stored.get("ssh_host"):
        stored["ssh_host"] = "localhost"
    return stored


def write_tunnel_env(health_listen_addr: str) -> Path:
    key = tunnel.load_runtime_key()
    path = Path.home() / ".livingruntime" / "tunnel.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"CONTROL_PLANE_TUNNEL_ID={tunnel.resolve_tunnel_id()}",
        f"CONTROL_PLANE_ORGANIZATION_ID={(os.environ.get('CONTROL_PLANE_ORGANIZATION_ID') or str(tunnel.load_config().get('tunnel_organization_id') or '')).strip()}",
        f"CONTROL_PLANE_API_KEY={key}",
        f"HEALTH_LISTEN_ADDR={health_listen_addr}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def ensure_localhost_ssh() -> None:
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    identity = ssh_dir / "id_ed25519"
    if not identity.exists():
        subprocess.check_call(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(identity), "-C", "livingruntime-localhost"]
        )
    pub = (ssh_dir / "id_ed25519.pub").read_text(encoding="utf-8").strip()
    authorized = ssh_dir / "authorized_keys"
    existing = authorized.read_text(encoding="utf-8") if authorized.exists() else ""
    if pub not in existing.splitlines():
        with authorized.open("a", encoding="utf-8") as handle:
            handle.write(pub + "\n")
    authorized.chmod(0o600)
    scan = subprocess.run(
        ["ssh-keyscan", "-t", "ed25519,ecdsa,rsa", "localhost", "127.0.0.1"],
        capture_output=True,
        text=True,
        check=False,
    )
    known = ssh_dir / "known_hosts"
    known_text = known.read_text(encoding="utf-8") if known.exists() else ""
    with known.open("a", encoding="utf-8") as handle:
        for line in (scan.stdout or "").splitlines():
            if line and line not in known_text:
                handle.write(line + "\n")
    known.chmod(0o600)
    probe = subprocess.run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "localhost",
            "true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise SystemExit("localhost SSH failed after key setup:\n" + (probe.stderr or probe.stdout))
    print("SSH OK: localhost")


def tar_plugin(source: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as bundle:
        for item in source.iterdir():
            if item.name in SKIP_DIR_NAMES:
                continue
            bundle.add(item, arcname=item.name, filter=_tar_filter)
    return buf.getvalue()


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = info.name.replace("\\", "/")
    parts = set(name.split("/"))
    if parts & SKIP_DIR_NAMES:
        return None
    if Path(name).suffix in SKIP_SUFFIXES:
        return None
    info.name = name
    return info


def stop_windows_local_tunnel() -> None:
    if os.name != "nt":
        return
    try:
        import companion

        companion.uninstall_task()
    except Exception as exc:
        print(f"Windows companion task was not removed: {exc}")
    subprocess.run(["taskkill", "/F", "/IM", "tunnel-client.exe"], capture_output=True, text=True)


def wait_ready(health_listen_addr: str, timeout: float = 30.0) -> None:
    url = f"http://{health_listen_addr}/readyz"
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                last = response.read().decode("utf-8", errors="replace")
                if "ready" in last.lower():
                    print(f"tunnel-client ready on {health_listen_addr}")
                    return
        except OSError as exc:
            last = str(exc)
        time.sleep(0.5)
    raise SystemExit(f"tunnel-client did not become ready at {url}: {last}")


def install_local(health_listen_addr: str) -> None:
    if os.name == "nt":
        raise SystemExit("install-host --local is for the Ubuntu LivingRuntime host, not Windows.")
    ensure_localhost_ssh()
    config_path = tunnel.config_path()
    if config_path.exists():
        config_path.write_text(
            json.dumps(localize_config(tunnel.load_config()), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        config_path.chmod(0o600)
    write_tunnel_env(health_listen_addr)
    python = ensure_runtime()
    os.environ["CONTROL_PLANE_API_KEY"] = tunnel.load_runtime_key()
    subprocess.check_call(
        [
            str(python),
            str(Path(__file__).resolve().with_name("tunnel.py")),
            "--install-client",
            "--init-only",
            "--force",
            "--health-listen-addr",
            health_listen_addr,
        ]
    )
    client = tunnel.find_tunnel_client()
    if client is None:
        raise SystemExit("tunnel-client was not installed on the host")
    unit_path = Path("/etc/systemd/system") / f"{SERVICE_NAME}.service"
    unit = systemd_unit(client=client)
    subprocess.run(
        ["sudo", "-n", "tee", str(unit_path)],
        input=unit.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.check_call(["sudo", "-n", "systemctl", "daemon-reload"])
    subprocess.check_call(["sudo", "-n", "systemctl", "enable", "--now", SERVICE_NAME])
    wait_ready(health_listen_addr)
    print(f"Enabled systemd unit {SERVICE_NAME}. Windows does not need to run tunnel-client.")


def push_to_host(host: str, health_listen_addr: str) -> None:
    source = plugin_source()
    if not (source / "plugin.json").exists():
        raise SystemExit(f"plugin.json not found in {source}")
    key = tunnel.runtime_key_path()
    if not key.exists():
        raise SystemExit(f"missing {key}; copy the restricted Tunnels key there first")
    stop_windows_local_tunnel()
    ssh_run(host, "mkdir -p ~/.livingruntime/plugin ~/.livingruntime/bin")
    ssh_run(host, "tar xzf - -C ~/.livingruntime/plugin", stdin=tar_plugin(source))
    subprocess.check_call(["scp", str(key), f"{host}:.livingruntime/control-plane.key"])
    ssh_run(host, "chmod 600 ~/.livingruntime/control-plane.key")
    remote_config = json.dumps(localize_config(tunnel.load_config()), ensure_ascii=False, indent=2) + "\n"
    ssh_run(host, "umask 077 && cat > ~/.livingruntime/remote.json", stdin=remote_config.encode("utf-8"))
    completed = ssh_run(
        host,
        "python3 ~/.livingruntime/plugin/scripts/install-host.py --local "
        f"--health-listen-addr {health_listen_addr}",
    )
    sys.stdout.buffer.write(completed.stdout)
    if completed.stderr:
        sys.stderr.buffer.write(completed.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run LivingRuntime Secure MCP Tunnel on the Ubuntu host instead of Windows."
    )
    parser.add_argument("--ssh", metavar="HOST", help="SSH alias of the LivingRuntime host, for example livingruntime-vm")
    parser.add_argument("--local", action="store_true", help="Install systemd tunnel-client on this Ubuntu host")
    parser.add_argument("--health-listen-addr", default=HOST_HEALTH_ADDR)
    args = parser.parse_args(argv)
    health = tunnel.validate_health_listen_addr(args.health_listen_addr)
    if args.ssh:
        push_to_host(args.ssh, health)
        return
    if args.local:
        install_local(health)
        return
    parser.print_help()
    raise SystemExit(2)


if __name__ == "__main__":
    main()
