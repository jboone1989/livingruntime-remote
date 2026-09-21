from __future__ import annotations

import os
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


def main() -> None:
    ensure_windows_ssh_env()
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
