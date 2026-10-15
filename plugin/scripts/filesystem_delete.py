"""Shared, packaged worker source for bounded filesystem deletion."""

DELETE_WORKER_SOURCE = r'''
"""Bounded POSIX deletion shared by SSH and native local Connector execution."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat

MAX_ENTRIES = 10000
MAX_BYTES = 512 * 1024 * 1024
MAX_DEPTH = 64


def _key(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _within(path, parent):
    return path == parent or parent in path.parents


def delete_path(path, roots, *, recursive=False, dry_run=False,
                expected_sha256=None, protected_paths=()):
    """Delete one literal path; never follow symlinks or cross filesystem mounts.

    Limits are checked for the entire tree before mutation. Configured roots,
    configured project roots and Connector/credential state cannot be deleted.
    Unsupported platforms fail closed instead of using unsafe path-based recursion.
    """
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise NotImplementedError("delete_path currently requires POSIX no-follow directory handles")
    value = Path(str(path)).expanduser()
    if not value.is_absolute() or ".." in value.parts:
        raise ValueError("delete_path requires an absolute path without parent-directory segments")
    configured = [Path(p).expanduser().resolve(strict=True) for p in roots]
    if not configured:
        raise PermissionError("no configured roots")
    if value == Path("/"):
        raise PermissionError("filesystem root cannot be deleted")
    home = Path.home()
    private_state = [
        home / name for name in (".ssh", ".livingruntime", ".codex", ".gnupg", ".config")
    ]
    protected = configured + private_state
    for item in protected_paths:
        p = Path(item).expanduser()
        protected.extend((p, p.resolve(strict=False)))
    for p in protected:
        if value == p or value in p.parents:
            raise PermissionError("configured or protected root cannot be deleted")
    for p in private_state:
        if _within(value, p):
            raise PermissionError("protected Connector, credential or project state")
    # Project descendants remain editable; only their root/ancestors are protected.
    # The home state directories above are protected recursively.
    matches = [root for root in configured if _within(value, root)]
    if not matches:
        raise PermissionError("path outside configured roots")
    root = max(matches, key=lambda p: len(p.parts))
    parts = value.relative_to(root).parts
    if not parts:
        raise PermissionError("configured root cannot be deleted")
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("expected_sha256 must contain 64 hexadecimal characters")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    handles = []
    nodes = {}
    planned = {"entries": 0, "bytes": 0}
    removed = {"entries": 0, "bytes": 0}
    try:
        parent = os.open(root, flags)
        handles.append(parent)
        root_device = os.fstat(parent).st_dev
        for part in parts[:-1]:
            try:
                parent = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                if expected_sha256 is not None:
                    raise RuntimeError("expected_sha256 supplied but path does not exist")
                return {"path": str(value), "status": "NOT_FOUND", "deleted": False,
                        "dry_run": bool(dry_run), "deleted_entries": 0, "deleted_bytes": 0}
            handles.append(parent)
            if os.fstat(parent).st_dev != root_device:
                raise PermissionError("deletion cannot cross a filesystem mount")
        name = parts[-1]
        try:
            initial = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if expected_sha256 is not None:
                raise RuntimeError("expected_sha256 supplied but path does not exist")
            return {"path": str(value), "status": "NOT_FOUND", "deleted": False,
                    "dry_run": bool(dry_run), "deleted_entries": 0, "deleted_bytes": 0}
        if stat.S_ISDIR(initial.st_mode) and not recursive:
            raise ValueError("directory deletion requires recursive=true")
        if expected_sha256 is not None and not stat.S_ISREG(initial.st_mode):
            raise ValueError("expected_sha256 is only supported for regular files")

        def inspect_entry(directory, entry, relative):
            info = os.stat(entry, dir_fd=directory, follow_symlinks=False)
            if info.st_dev != root_device:
                raise PermissionError("deletion cannot cross a filesystem mount")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)):
                raise PermissionError("special files cannot be deleted")
            if len(relative) > MAX_DEPTH:
                raise ValueError("directory depth exceeds deletion limit")
            planned["entries"] += 1
            planned["bytes"] += info.st_size if stat.S_ISREG(info.st_mode) else 0
            if planned["entries"] > MAX_ENTRIES or planned["bytes"] > MAX_BYTES:
                raise ValueError("tree exceeds bounded deletion limits")
            children = None
            if stat.S_ISDIR(info.st_mode):
                fd = os.open(entry, flags, dir_fd=directory)
                try:
                    if _key(os.fstat(fd)) != _key(info):
                        raise RuntimeError("path changed during deletion preflight")
                    children = sorted(os.listdir(fd))
                    for child in children:
                        inspect_entry(fd, child, relative + (child,))
                    if _key(os.fstat(fd)) != _key(info):
                        raise RuntimeError("directory changed during deletion preflight")
                finally:
                    os.close(fd)
            nodes[relative] = (info, children)

        inspect_entry(parent, name, (name,))
        observed_sha256 = None
        if expected_sha256 is not None:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if _key(os.fstat(fd)) != _key(initial):
                    raise RuntimeError("file changed before hash verification")
                digest = hashlib.sha256()
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                observed_sha256 = digest.hexdigest()
                if _key(os.fstat(fd)) != _key(initial):
                    raise RuntimeError("file changed during hash verification")
            finally:
                os.close(fd)
            if observed_sha256 != expected_sha256.lower():
                raise RuntimeError("sha256 mismatch")
        result = {"path": str(value), "dry_run": bool(dry_run),
                  "planned_entries": planned["entries"], "planned_bytes": planned["bytes"],
                  "deleted_entries": 0, "deleted_bytes": 0, "deleted": False}
        if observed_sha256 is not None:
            result["sha256"] = observed_sha256
        if dry_run:
            return {**result, "status": "DRY_RUN"}

        def remove_entry(directory, entry, relative):
            before, children = nodes[relative]
            now = os.stat(entry, dir_fd=directory, follow_symlinks=False)
            if _key(now) != _key(before):
                raise RuntimeError("path changed since deletion preflight")
            if children is not None:
                fd = os.open(entry, flags, dir_fd=directory)
                try:
                    if _key(os.fstat(fd)) != _key(before) or sorted(os.listdir(fd)) != children:
                        raise RuntimeError("directory changed since deletion preflight")
                    for child in children:
                        remove_entry(fd, child, relative + (child,))
                    # Reject replacement of the directory entry before rmdir.
                    current = os.stat(entry, dir_fd=directory, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                        raise RuntimeError("directory replaced during deletion")
                    os.rmdir(entry, dir_fd=directory)
                finally:
                    os.close(fd)
            else:
                os.unlink(entry, dir_fd=directory)
            removed["entries"] += 1
            removed["bytes"] += before.st_size if stat.S_ISREG(before.st_mode) else 0

        try:
            remove_entry(parent, name, (name,))
        except (OSError, RuntimeError) as exc:
            return {**result, "status": "PARTIAL_FAILURE",
                    "deleted_entries": removed["entries"], "deleted_bytes": removed["bytes"],
                    "error": type(exc).__name__ + ": " + str(exc)}
        return {**result, "status": "DELETED", "deleted": True,
                "deleted_entries": removed["entries"], "deleted_bytes": removed["bytes"]}
    finally:
        for fd in reversed(handles):
            os.close(fd)
'''

exec(compile(DELETE_WORKER_SOURCE, '<bounded-filesystem-delete>', 'exec'), globals())
