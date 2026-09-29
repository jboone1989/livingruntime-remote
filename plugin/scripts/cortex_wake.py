from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_AGENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,240}$")
_DEFAULT_MARKER = "FERRO_CORTEX_WAKE.json"


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _config() -> dict[str, Any]:
    raw_path = _env("LIVINGRUNTIME_COGNITION_GITHUB_WAKE_CONFIG")
    path = (
        Path(raw_path).expanduser()
        if raw_path
        else Path.home() / ".livingruntime" / "cognition-github-wake.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("invalid cognition GitHub wake config")
    return value


def _setting(env_name: str, config_key: str, default: str = "") -> str:
    if env_name in os.environ:
        return str(os.environ.get(env_name) or "").strip()
    return str(_config().get(config_key) or default).strip()


def _configured_repo() -> Path | None:
    raw = _setting("LIVINGRUNTIME_COGNITION_GITHUB_WAKE_REPO", "repo")
    return Path(raw).expanduser().resolve() if raw else None


def _branch() -> str:
    value = _setting(
        "LIVINGRUNTIME_COGNITION_GITHUB_WAKE_BRANCH",
        "branch",
        "livingruntime/ferro-cortex-wake",
    )
    if not _BRANCH_RE.fullmatch(value) or ".." in value or value.startswith("/") or value.endswith("/"):
        raise ValueError("invalid cognition GitHub wake branch")
    return value


def _agent_filter() -> str:
    value = _setting(
        "LIVINGRUNTIME_COGNITION_GITHUB_WAKE_AGENT",
        "agent",
        "ferro",
    )
    if not _AGENT_RE.fullmatch(value):
        raise ValueError("invalid cognition GitHub wake agent")
    return value


def _marker_name() -> str:
    value = _setting(
        "LIVINGRUNTIME_COGNITION_GITHUB_WAKE_FILE",
        "file",
        _DEFAULT_MARKER,
    )
    if value != Path(value).name or value in {".", ".."}:
        raise ValueError("cognition GitHub wake file must be a repo-root filename")
    return value


def _remote() -> str:
    value = _setting(
        "LIVINGRUNTIME_COGNITION_GITHUB_WAKE_REMOTE",
        "remote",
        "origin",
    )
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise ValueError("invalid cognition GitHub wake remote")
    return value


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "GitHub cognition wake git command failed"
            + (f": {detail[-1000:]}" if detail else "")
        )
    return completed.stdout.strip()


@contextmanager
def _wake_lock(repo: Path) -> Iterator[None]:
    raw_path = _run_git(
        repo,
        "rev-parse",
        "--git-path",
        "livingruntime-cognition-wake.lock",
    )
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo / path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_marker(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError("invalid cognition GitHub wake marker")
    return value


def emit(request: dict[str, Any]) -> dict[str, Any]:
    """Emit one privacy-bounded GitHub PR activity event for a cognition request."""
    repo = _configured_repo()
    if repo is None:
        return {"status": "DISABLED", "transport": "github-pr-commit"}
    if not repo.is_dir() or not (repo / ".git").exists():
        raise RuntimeError("configured cognition GitHub wake repo is not a Git worktree")

    agent_id = str(request.get("agent_id") or "").strip()
    if agent_id != _agent_filter():
        return {
            "status": "IGNORED_AGENT",
            "transport": "github-pr-commit",
            "agent_id": agent_id,
        }

    request_id = str(request.get("request_id") or "").strip()
    if not request_id.startswith("llmreq_"):
        raise ValueError("cognition wake requires a durable request_id")

    branch = _branch()
    marker_name = _marker_name()
    marker_path = repo / marker_name
    remote = _remote()

    with _wake_lock(repo):
        current_branch = _run_git(repo, "branch", "--show-current")
        if current_branch != branch:
            raise RuntimeError(
                f"cognition GitHub wake worktree is on {current_branch!r}, expected {branch!r}"
            )

        dirty = [
            line
            for line in _run_git(repo, "status", "--porcelain").splitlines()
            if line.strip() and not line.endswith(" " + marker_name)
        ]
        if dirty:
            raise RuntimeError("cognition GitHub wake worktree contains unrelated changes")

        previous = _load_marker(marker_path)
        if str(previous.get("request_id") or "") == request_id:
            return {
                "status": "ALREADY_EMITTED",
                "transport": "github-pr-commit",
                "request_id": request_id,
                "branch": branch,
            }

        sequence = int(previous.get("wake_sequence") or 0) + 1
        marker = {
            "schema_version": "livingruntime-cortex-wake.v1",
            "event": "FERRO_CORTEX_WAKE",
            "agent_id": agent_id,
            "request_id": request_id,
            "wake_sequence": sequence,
        }
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _run_git(repo, "add", "--", marker_name)
        _run_git(repo, "commit", "-m", f"wake: {agent_id} {request_id}")
        _run_git(repo, "push", remote, f"HEAD:{branch}")
        commit_sha = _run_git(repo, "rev-parse", "HEAD")
        return {
            "status": "EMITTED",
            "transport": "github-pr-commit",
            "request_id": request_id,
            "branch": branch,
            "commit_sha": commit_sha,
            "wake_sequence": sequence,
        }


__all__ = ["emit"]
