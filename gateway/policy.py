from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


class CommandPolicyError(ValueError):
    pass


ALLOWED_EXECUTABLES = {
    "bash",
    "cargo",
    "df",
    "du",
    "find",
    "go",
    "head",
    "make",
    "node",
    "npm",
    "npx",
    "pnpm",
    "ps",
    "pytest",
    "python",
    "python3",
    "ruff",
    "sed",
    "ss",
    "tail",
    "uv",
}

BLOCKED_EXEC_TOKENS = {"-c", "-lc", "--command"}
BLOCKED_GIT_SUBCOMMANDS = {"credential", "daemon", "shell"}
BLOCKED_GIT_FLAGS = {"--config-env", "-c", "-C", "--git-dir", "--work-tree"}


def resolve_allowed_path(path: str, roots: Iterable[Path], *, must_exist: bool) -> Path:
    candidate = Path(path).expanduser()
    if must_exist or candidate.exists() or candidate.is_symlink():
        resolved = candidate.resolve(strict=True)
    else:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
    for root in roots:
        root = root.resolve(strict=True)
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PermissionError(f"path is outside allowed roots: {resolved}")


def validate_exec_argv(argv: list[str]) -> None:
    if not argv or not isinstance(argv[0], str):
        raise CommandPolicyError("argv must contain an executable")
    executable = os.path.basename(argv[0])
    if executable not in ALLOWED_EXECUTABLES:
        raise CommandPolicyError(f"executable is not allowlisted: {executable}")
    if executable == "bash" and any(token in BLOCKED_EXEC_TOKENS for token in argv[1:]):
        raise CommandPolicyError("bash -c/-lc is blocked; pass a script path instead")
    if any("\x00" in str(token) for token in argv):
        raise CommandPolicyError("NUL bytes are not allowed")


def validate_git_args(args: list[str]) -> None:
    if not args:
        raise CommandPolicyError("git args are required")
    subcommand = next((x for x in args if not x.startswith("-")), "")
    if not subcommand:
        raise CommandPolicyError("git subcommand is required")
    if subcommand in BLOCKED_GIT_SUBCOMMANDS:
        raise CommandPolicyError(f"git subcommand is blocked: {subcommand}")
    if any(
        arg in BLOCKED_GIT_FLAGS
        or arg.startswith("--git-dir=")
        or arg.startswith("--work-tree=")
        for arg in args
    ):
        raise CommandPolicyError("unsafe git path/config flag is blocked")
    joined = " ".join(args)
    if "credential.helper" in joined or "core.sshCommand" in joined:
        raise CommandPolicyError("credential and ssh command overrides are blocked")


def validate_systemd_action(action: str, unit: str, allowed_units: set[str]) -> None:
    if action not in {"status", "is-active", "start", "stop", "restart"}:
        raise CommandPolicyError("unsupported systemd action")
    if unit not in allowed_units:
        raise PermissionError("unit is not allowlisted")
