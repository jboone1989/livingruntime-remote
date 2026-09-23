from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

MCP_VERSION = "1.26.0"


def runtime_root() -> Path:
    return Path.home() / ".livingruntime" / "mcp-runtime"


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "venv" / "Scripts" / "python.exe"
    return root / "venv" / "bin" / "python"


def ensure_runtime() -> Path:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    python = venv_python(root)
    marker = root / f"mcp-{MCP_VERSION}.installed"

    if not python.exists():
        venv.EnvBuilder(with_pip=True, clear=False).create(root / "venv")

    if not marker.exists():
        subprocess.check_call(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"mcp=={MCP_VERSION}",
            ],
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        for stale in root.glob("mcp-*.installed"):
            stale.unlink(missing_ok=True)
        marker.write_text(MCP_VERSION + "\n", encoding="utf-8")

    return python


def ensure_windows_ssh_env() -> None:
    """Windows OpenSSH exits 255 with empty stderr if ProgramData is missing."""
    if os.name != "nt" or os.environ.get("ProgramData"):
        return
    drive = os.environ.get("SYSTEMDRIVE") or "C:"
    os.environ["ProgramData"] = drive.rstrip("\\/") + r"\ProgramData"


def _node_binary() -> Path | None:
    explicit = str(os.environ.get("LIVINGRUNTIME_NODE") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    found = shutil.which("node")
    if found:
        return Path(found)
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    candidates = [path for path in nvm_root.glob("*/bin/node") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def start_host_worker_launcher() -> bool:
    """Run one optional same-user launcher that detaches host-owned workers."""
    if os.name == "nt":
        return False
    launcher = Path.home() / ".livingruntime" / "bin" / "host-worker-launcher.mjs"
    if not launcher.is_file():
        return False
    node = _node_binary()
    if node is None:
        print("LivingRuntime host worker launcher skipped: node not found", file=sys.stderr)
        return False
    try:
        completed = subprocess.run(
            [str(node), str(launcher)],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=5,
            check=False,
            env=os.environ.copy(),
        )
    except Exception as exc:
        print(
            f"LivingRuntime host worker launcher skipped: {type(exc).__name__}",
            file=sys.stderr,
        )
        return False
    if completed.returncode != 0:
        print(
            f"LivingRuntime host worker launcher exited {completed.returncode}",
            file=sys.stderr,
        )
        return False
    return True


def main() -> None:
    ensure_windows_ssh_env()
    start_host_worker_launcher()
    python = ensure_runtime()
    bridge = Path(__file__).resolve().with_name("bridge.py")
    raise SystemExit(
        subprocess.call(
            [str(python), str(bridge), *sys.argv[1:]],
            stdin=None,
            stdout=None,
            stderr=None,
            env=os.environ.copy(),
        )
    )


if __name__ == "__main__":
    main()
