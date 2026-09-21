from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path

from configmodel import (
    DEFAULT_HOST_ID,
    DEFAULT_ROOT,
    normalize,
    posix_path,
    seed_projects,
    to_storage,
)


def config_path() -> Path:
    return Path.home() / ".livingruntime" / "remote.json"


def validate_host(value: str) -> str:
    value = value.strip()
    if not value or value.startswith("-") or any(ch.isspace() for ch in value):
        raise argparse.ArgumentTypeError("host must be an SSH config alias or host token without whitespace")
    return value


def test_ssh(host: str) -> None:
    remote_command = shlex.join(
        [
            "python3",
            "-c",
            "import getpass,socket; print(getpass.getuser()+'@'+socket.gethostname())",
        ]
    )
    proc = subprocess.run(
        [
            "ssh", "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10",
            host,
            remote_command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            "SSH verification failed. Configure key-based SSH first and verify the host key.\n"
            + (proc.stderr or proc.stdout)
        )
    print("SSH OK:", proc.stdout.strip())


def load_existing(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("remote config must be a JSON object")
    return value


def write_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure LivingRuntime Remote without storing SSH passwords or private keys."
    )
    parser.add_argument("--host", type=validate_host, help="SSH config alias, e.g. livingruntime-vm")
    parser.add_argument("--host-id", default=DEFAULT_HOST_ID, help="Logical host id, default main")
    parser.add_argument("--root", action="append", dest="roots", help="Allowed remote workspace root; repeatable")
    parser.add_argument("--unit", action="append", dest="units", help="Allowlisted systemd .service unit; repeatable")
    parser.add_argument("--project", help="Add or update a named project")
    parser.add_argument("--project-path", help="Remote path for --project")
    parser.add_argument("--project-unit", action="append", dest="project_units", help="Allowlisted unit for --project; repeatable")
    parser.add_argument("--seed-projects", action="store_true", help="Add default agent-runtime/virtualbrain/ferro/trading projects")
    parser.add_argument("--tunnel-id", dest="tunnel_id", help="OpenAI Secure MCP Tunnel id (non-secret); never store the runtime API key here")
    parser.add_argument("--test", action="store_true", help="Verify BatchMode SSH after writing configuration")
    parser.add_argument("--show", action="store_true", help="Show the current non-secret configuration")
    args = parser.parse_args()

    path = config_path()
    existing = load_existing(path)
    if args.show:
        if not existing:
            raise SystemExit(f"No configuration at {path}")
        print(json.dumps(to_storage(normalize(existing)), ensure_ascii=False, indent=2))
        return

    if not existing and not args.host:
        parser.error("--host is required when creating a new configuration")

    if existing:
        cfg = normalize(existing)
    else:
        cfg = normalize(
            {
                "ssh_host": args.host,
                "roots": args.roots or [DEFAULT_ROOT],
                "systemd_units": args.units or [],
            }
        )

    host_id = str(args.host_id or DEFAULT_HOST_ID)
    host = dict(cfg["hosts"].get(host_id) or {"id": host_id, "ssh_host": "", "roots": [DEFAULT_ROOT], "units": []})
    if args.host:
        host["ssh_host"] = args.host
        host["id"] = host_id
    if args.roots:
        host["roots"] = [posix_path(x) for x in args.roots]
    if args.units is not None:
        host["units"] = [str(x).strip() for x in args.units if str(x).strip()]
    if not host.get("ssh_host"):
        parser.error(f"host {host_id} is missing ssh_host")
    cfg["hosts"][host_id] = host
    if host_id not in cfg["hosts"] or cfg.get("default_host") not in cfg["hosts"]:
        cfg["default_host"] = host_id

    if args.tunnel_id:
        tunnel_id = args.tunnel_id.strip()
        if not tunnel_id.startswith("tunnel_"):
            raise SystemExit("tunnel id must start with tunnel_ and must not be an API key")
        cfg["tunnel_id"] = tunnel_id

    if args.seed_projects or not cfg["projects"]:
        cfg = seed_projects(cfg)

    if args.project:
        if not args.project_path:
            parser.error("--project-path is required with --project")
        cfg["projects"][args.project] = {
            "name": args.project,
            "host": host_id,
            "path": posix_path(args.project_path),
            "units": [str(x).strip() for x in (args.project_units or []) if str(x).strip()],
        }

    cfg = normalize(to_storage(cfg))
    payload = to_storage(cfg)
    write_config(path, payload)
    print(f"Wrote non-secret configuration to {path}")
    print("SSH credentials remain in your normal SSH config/agent; they are not copied into the plugin.")
    if payload.get("projects"):
        print("Projects:", ", ".join(sorted(payload["projects"])))

    if args.test:
        test_ssh(host["ssh_host"])


if __name__ == "__main__":
    main()
