"""Export a Pi repair from an isolated worktree without GitHub credentials.

The model edits a local checkout. This exporter reads actual git changes,
pins each source file to the original PR head, and emits a bounded manifest.
Only the trusted central writer may use GitHub credentials or update the PR.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from eventgraph_pi_patch_writer import MAX_FILES, REPOSITORY, SHA, safe_path, validate


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], check=True,
                            capture_output=True, timeout=15)
    return result.stdout


def export(root, pr_number, expected_sha):
    root = Path(root).resolve()
    if not SHA.fullmatch(expected_sha):
        raise ValueError("invalid expected PR HEAD")
    if git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise ValueError("worktree must be its Git root")
    # Allow Pi to commit locally, but only if its HEAD descends from the
    # exact original PR revision, not a different checkout or unrelated tree.
    git(root, "merge-base", "--is-ancestor", expected_sha, "HEAD")
    changed = set(git(root, "diff", "--name-only", "-z", expected_sha).decode().strip("\x00").split("\x00"))
    changed.update(git(root, "ls-files", "--others", "--exclude-standard", "-z")
                   .decode().strip("\x00").split("\x00"))
    changed.discard("")
    if not 1 <= len(changed) <= MAX_FILES:
        raise ValueError("no changes or too many changed files")
    changes = []
    for raw in sorted(changed):
        path = safe_path(raw)
        candidate = root / path
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("missing, deleted or symbolic-link repair file")
        # The only known legal existing mode is ordinary text, not executable.
        base = git(root, "ls-tree", expected_sha, "--", path).decode().strip()
        expected_blob = None
        if base:
            fields = base.split(None, 3)
            if len(fields) != 4 or fields[0:2] != ["100644", "blob"]:
                raise ValueError("unsupported original file mode")
            expected_blob = fields[2]
        raw_bytes = candidate.read_bytes()
        if b"\x00" in raw_bytes:
            raise ValueError("binary patch content is unsupported")
        content = raw_bytes.decode("utf-8")
        changes.append({"path": path, "expected_blob_sha": expected_blob, "content": content})
    manifest = {"repository": REPOSITORY, "pr_number": pr_number,
                "expected_head_sha": expected_sha, "changes": changes}
    validate(manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = export(args.worktree, args.pr_number, args.expected_head_sha)
    out = Path(args.output)
    if out.exists():
        raise ValueError("manifest already exists; refusing overwrite")
    out.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print("exported", len(result["changes"]), "files from", args.expected_head_sha[:12])


if __name__ == "__main__":
    main()
