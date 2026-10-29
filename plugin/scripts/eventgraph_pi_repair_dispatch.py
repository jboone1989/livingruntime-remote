"""Trusted central routing: EventGraph failed PR -> paired cn-dev native Pi.

Only PR/head SHA and failure identifiers cross the Relay. The reviewer remains
independent, and no review status is fabricated by this process.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPOSITORY = "jboone1989/EventGraph"
SHA = re.compile(r"^[0-9a-f]{40}$")
MARKER = re.compile(
    r"<!-- eventgraph-pi-repair sha=([0-9a-f]{40}) attempt=([1-3]) state=([a-z-]+) -->"
)
MAX_ROUNDS = 3
GH = "/home/ubuntu/.local/bin/gh"


def api(path: str, *, method: str = "GET", body: str | None = None):
    argv = [GH, "api"]
    if method != "GET":
        argv += ["-X", method]
    argv.append(path)
    if body is not None:
        argv += ["-f", "body=" + body]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    if result.returncode != 0:
        raise RuntimeError("GitHub API " + method + " failed for a trusted endpoint")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def validate_failure(pr: dict, sha: str, kind: str, run_id: int | None):
    if (pr.get("state") != "open" or pr.get("draft") or
        pr.get("base", {}).get("ref") != "develop" or
        pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY or
        pr.get("head", {}).get("sha") != sha):
        raise ValueError("PR is ineligible or its latest head changed")
    if kind == "check":
        if not isinstance(run_id, int) or run_id < 1:
            raise ValueError("missing failed Check run ID")
        run = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
        linked = run.get("pull_requests") or []
        origin = run.get("head_repository")
        if (run.get("head_sha") != sha or run.get("event") != "pull_request" or
            run.get("conclusion") != "failure" or run.get("name") != "Check" or
            (origin and origin.get("full_name") != REPOSITORY) or
            (linked and not any(item.get("number") == pr.get("number")
                                for item in linked))):
            raise ValueError("Check run is not a verified failure for this PR and SHA")
    elif kind == "claude-review":
        statuses = api(f"repos/{REPOSITORY}/commits/{sha}/status").get("statuses", [])
        recent = next((s for s in statuses if s.get("context") == "claude-review"), None)
        if not recent or recent.get("state") not in {"failure", "error"}:
            raise ValueError("latest independent review is not blocking at this SHA")
    else:
        raise ValueError("unrecognized failure type")


def read_markers(number: int):
    markers = []
    for page in range(1, 11):
        comments = api(
            f"repos/{REPOSITORY}/issues/{number}/comments?per_page=100&page={page}"
        )
        for item in comments:
            # PR comments are untrusted: a forged ledger marker must not exhaust
            # repair rounds or suppress dispatch for a real failing HEAD.
            author = item.get("user") or {}
            trusted = item.get("author_association") in ("OWNER", "MEMBER", "COLLABORATOR")
            if not trusted and author.get("login") != "github-actions[bot]":
                continue
            match = MARKER.match(str(item.get("body", "")))
            if match:
                markers.append((match.group(1), int(match.group(2)), match.group(3)))
        if len(comments) < 100:
            return markers
    raise RuntimeError("too many PR comments to safely deduplicate")


def next_attempt(markers, sha):
    if any(row[0] == sha and row[2] in ("started", "dispatched") for row in markers):
        return None
    attempts = {row[0] for row in markers if row[2] in ("started", "dispatched")}
    if len(attempts) >= MAX_ROUNDS:
        return 0
    return len(attempts) + 1


def dispatch(number: int, sha: str, kind: str, run_id: int | None):
    cfg_path = Path.home() / ".livingruntime" / "relay.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    url = str(cfg["url"]).rstrip("/") + "/device/pi-repair"
    token = str(cfg["device_token"])
    payload = json.dumps({
        "repository": REPOSITORY, "pr_number": number, "sha": sha,
        "failure_kind": kind, "run_id": run_id,
    }).encode()
    request = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            received = json.loads(response.read(8192))
    except Exception as exc:
        # Never expose URL, headers, token, or any untrusted upstream body.
        raise RuntimeError("cn-dev Pi dispatch unavailable: " + type(exc).__name__) from None
    if not received.get("ok") or not received.get("session_id"):
        raise RuntimeError("cn-dev Pi did not return an active session")
    return received["session_id"]


def run(number: int, sha: str, kind: str, run_id: int | None):
    if not 1 <= number <= 100000 or not SHA.fullmatch(sha):
        raise ValueError("invalid EventGraph PR number or SHA")
    pr = api(f"repos/{REPOSITORY}/pulls/{number}")
    validate_failure(pr, sha, kind, run_id)
    attempt = next_attempt(read_markers(number), sha)
    if attempt is None:
        print("Already dispatched for this exact PR head; skipped")
        return
    if attempt == 0:
        print("Repair limit reached; review and PR remain blocked")
        return
    marker = f"<!-- eventgraph-pi-repair sha={sha} attempt={attempt} state=started -->"
    comment = api(f"repos/{REPOSITORY}/issues/{number}/comments", method="POST",
                  body=marker + "\nRequesting cn-dev native Pi repair; this is NOT a completed fix.")
    try:
        session = dispatch(number, sha, kind, run_id)
    except Exception as exc:
        # The attempt was not launched. A repeated event may retry this SHA.
        api(f"repos/{REPOSITORY}/issues/comments/{comment['id']}", method="PATCH",
            body=f"<!-- eventgraph-pi-repair sha={sha} attempt={attempt} state=worker-unavailable -->\n"
                 + "cn-dev Pi could not start. The existing CI/Claude gates remain authoritative.")
        raise
    api(f"repos/{REPOSITORY}/issues/comments/{comment['id']}", method="PATCH",
        body=f"<!-- eventgraph-pi-repair sha={sha} attempt={attempt} state=dispatched -->\n"
             + f"cn-dev native Pi session \`{session}\` accepted the repair request. "
             + "This is NOT a claim that a patch was pushed or CI passed.")
    print("Dispatched cn-dev Pi for EventGraph PR #", number)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--failure-kind", required=True)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    if args.repository != REPOSITORY:
        raise ValueError("unsupported repository")
    rid = int(args.run_id) if args.run_id else None
    run(args.pr_number, args.sha, args.failure_kind, rid)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Pi repair handoff failed: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(1)
