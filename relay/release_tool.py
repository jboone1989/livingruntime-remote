from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path("/opt/livingruntime-remote-relay")
RELEASES = ROOT / "releases"
NAME_RE = re.compile(r"^v[0-9A-Za-z._-]+$")
SEED_FILES = ("auth.py", "embedded_auth.py", "store.py", "mcp_events.py", "contract.py", "requirements.txt", "README.md")
OPTIONAL_UI_FILES = (
    "secure_terminal_widget.html",
    "secure_input_widget.html",
    "credential_manager_widget.html",
)


def release_path(name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise SystemExit("invalid release name")
    path = RELEASES / name
    if path.parent.resolve(strict=True) != RELEASES.resolve(strict=True):
        raise SystemExit("release escapes release root")
    return path


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: release_tool.py prepare RELEASE | seed BASE RELEASE | activate RELEASE")
    action = sys.argv[1]
    if action == "prepare" and len(sys.argv) == 3:
        target = release_path(sys.argv[2])
        target.mkdir(mode=0o755, exist_ok=False)
        print(target)
        return
    if action == "seed" and len(sys.argv) == 4:
        base = release_path(sys.argv[2]).resolve(strict=True)
        target = release_path(sys.argv[3]).resolve(strict=True)
        for name in SEED_FILES:
            shutil.copy2(base / name, target / name)
        for name in OPTIONAL_UI_FILES:
            if (base / name).is_file():
                shutil.copy2(base / name, target / name)
        print(target)
        return
    if action == "activate" and len(sys.argv) == 3:
        name = sys.argv[2]
        target = release_path(name)
        if not target.is_dir():
            raise SystemExit("release does not exist")
        current = ROOT / "current"
        temp = ROOT / ".current.next"
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        temp.symlink_to(Path("releases") / name, target_is_directory=True)
        temp.replace(current)
        print(current.resolve(strict=True))
        return
    raise SystemExit("unsupported arguments")


if __name__ == "__main__":
    main()
