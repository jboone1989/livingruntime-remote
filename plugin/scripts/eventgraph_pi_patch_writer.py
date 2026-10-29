"""Trusted, atomic GitHub writer for a native Pi repair produced on cn-dev.

The untrusted agent supplies only a bounded UTF-8 file manifest. It never gets
this process's gh credentials. This script is intentionally NOT a model/agent
entrypoint; it must be called by an authenticated trusted handoff.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import PurePosixPath

REPOSITORY = "jboone1989/EventGraph"
SHA = re.compile(r"^[0-9a-f]{40}$")
ALLOWED = ("apps/web/app/features/", "apps/web/app/routes/", "apps/web/tests/",
           "packages/backend/src/editorial/", "packages/backend/src/sources/",
           "tests/")
FORBIDDEN = {"auth", "security", "credential", "credentials", "secret", "secrets",
             "config", "migration", "migrations", "ci", "deploy", "database", "db"}
MAX_FILES = 24
MAX_BYTES = 200_000
GH = "/home/ubuntu/.local/bin/gh"


def api(path, *, method="GET", body=None):
    args = [GH, "api", "repos/" + REPOSITORY + "/" + path]
    if method != "GET":
        args.extend(("-X", method))
    if body is not None:
        args.extend(("--input", "-"))
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, timeout=45, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def safe_path(raw):
    if not isinstance(raw, str) or not raw.isascii() or len(raw) > 240:
        raise ValueError("invalid UTF-8 file path")
    parts = PurePosixPath(raw).parts
    if (not raw.startswith(ALLOWED) or not parts or
        any(part in ("", ".", "..") or part.lower() in FORBIDDEN for part in parts) or
        "\\" in raw or raw.startswith("/") or any(part.startswith(".") for part in parts) or
        raw.endswith((".json", ".yml", ".yaml", ".sql", ".sh", ".mjs", ".cjs")) or
        not raw.endswith((".ts", ".tsx", ".py"))):
        raise ValueError("repair path is not eligible application/test code")
    return raw


def validate(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {
        "repository", "pr_number", "expected_head_sha", "changes"
    } or manifest["repository"] != REPOSITORY:
        raise ValueError("unexpected repair manifest")
    number, sha = manifest["pr_number"], manifest["expected_head_sha"]
    if type(number) is not int or not 1 <= number <= 100000 or not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("invalid PR identity or expected head")
    items = manifest["changes"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_FILES:
        raise ValueError("invalid bounded change count")
    names, total = set(), 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "expected_blob_sha", "content"}:
            raise ValueError("invalid change object")
        path = safe_path(item["path"])
        if path in names:
            raise ValueError("duplicate change path")
        names.add(path)
        blob = item["expected_blob_sha"]
        if blob is not None and (not isinstance(blob, str) or not SHA.fullmatch(blob)):
            raise ValueError("invalid base blob SHA")
        content = item["content"]
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("invalid UTF-8 source")
        total += len(content.encode("utf-8"))
        if total > MAX_BYTES:
            raise ValueError("patch payload too large")
    return number, sha, items


def current_pr(number, sha):
    pr = api("pulls/" + str(number))
    if (pr.get("state") != "open" or pr.get("draft") or
        pr.get("base", {}).get("ref") != "develop" or
        pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY or
        pr.get("head", {}).get("sha") != sha):
        raise PermissionError("PR closed, forked, ineligible or head has moved")
    branch = pr["head"].get("ref", "")
    if not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", branch) or ".." in branch or branch.startswith("-"):
        raise ValueError("invalid branch")
    return branch


def prepare_tree(sha, changes):
    commit = api("git/commits/" + sha)
    base_tree = commit["tree"]["sha"]
    tree = api("git/trees/" + base_tree + "?recursive=1")
    if tree.get("truncated") or not isinstance(tree.get("tree"), list):
        raise RuntimeError("cannot safely verify the complete base tree")
    existing = {item["path"]: item for item in tree["tree"] if item["type"] == "blob"}
    entries = []
    for item in changes:
        path, expected = item["path"], item["expected_blob_sha"]
        base = existing.get(path)
        if base is None:
            if expected is not None or "/tests/" not in "/" + path and not path.startswith("tests/"):
                raise ValueError("new files must be tests absent in the base")
        elif base["sha"] != expected or base["mode"] != "100644":
            raise PermissionError("file version changed or executable path")
        if base is not None and expected is None:
            raise PermissionError("existing file without pinned base blob")
        blob = api("git/blobs", method="POST", body={
            "content": item["content"], "encoding": "utf-8"})
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = api("git/trees", method="POST", body={
        "base_tree": base_tree, "tree": entries})
    return new_tree["sha"]


def write(manifest):
    number, sha, changes = validate(manifest)
    branch = current_pr(number, sha)
    tree_sha = prepare_tree(sha, changes)
    commit = api("git/commits", method="POST", body={
        "message": f"fix(pr-{number}): Pi repair of verified failure at {sha[:12]}",
        "tree": tree_sha, "parents": [sha]})
    # Ref update is a single non-force fast-forward. Concurrent changes cannot be
    # overwritten; only a new HEAD can naturally trigger existing CI/review.
    current_pr(number, sha)
    api("git/refs/heads/" + branch, method="PATCH",
        body={"sha": commit["sha"], "force": False})
    return {"pr_number": number, "previous_sha": sha, "new_sha": commit["sha"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    options = parser.parse_args()
    with open(options.manifest, encoding="utf-8") as stream:
        manifest = json.load(stream)
    print(json.dumps(write(manifest), sort_keys=True))


if __name__ == "__main__":
    main()
