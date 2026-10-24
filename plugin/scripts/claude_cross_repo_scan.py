#!/usr/bin/env python3
"""Review opt-in proposal Issues across owner repositories using ONE existing Runner.

No source-repository workflows, cross-repo dispatch secrets, or extra Claude workers.
Only explicitly labeled Issues or exact owner /devils-advocate comments qualify.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from urllib.parse import urlencode, urlparse

from claude_issue_review import COMMAND, MARKER, already_reviewed, gh_json, report_input


def search_issues(gh: str, owner: str) -> list[tuple[str, int]]:
    queries = [
        f"user:{owner} is:issue is:open label:devils-advocate",
        f'user:{owner} is:issue is:open in:comments "{COMMAND}"',
    ]
    candidates = {}
    for query in queries:
        # GitHub search queries are over repository ownership, not the Issue author.
        endpoint = "search/issues?" + urlencode({
            "q": query, "sort": "updated", "order": "desc", "per_page": 50,
        })
        for item in gh_json(gh, endpoint).get("items", []):
            path = urlparse(item.get("html_url", "")).path.strip("/").split("/")
            if len(path) != 4 or path[0].casefold() != owner.casefold() or path[2] != "issues":
                continue
            if path[3].isdigit():
                repo = f"{path[0]}/{path[1]}"
                candidates[(repo, int(path[3]))] = None
    return list(candidates)


def has_new_request(issue: dict, comments: list, owner: str, reporter: str) -> bool:
    if issue.get("state") != "open" or "pull_request" in issue:
        return False
    labeled = any(label.get("name", "").casefold() == "devils-advocate"
                  for label in issue.get("labels", []))
    owner_command = [
        c for c in comments
        if (c.get("user") or {}).get("login", "").casefold() == owner.casefold()
        and (c.get("body") or "").strip() == COMMAND
    ]
    if not labeled and not owner_command:
        return False

    trusted_reports = [
        c for c in comments
        if (c.get("user") or {}).get("login") in {"github-actions[bot]", reporter}
        and (c.get("body") or "").startswith(MARKER)
    ]
    if not trusted_reports:
        return True

    # A label is the initial request. Further reviews require a new explicit
    # owner slash-command after the last report (not arbitrary Issue chatter).
    newest_report = max((c.get("created_at") or "") for c in trusted_reports)
    return any((c.get("created_at") or "") > newest_report for c in owner_command)


def scan(owner: str, gh: str, claude: str, max_reviews: int, dry_run: bool) -> int:
    reporter = gh_json(gh, "user").get("login")
    if not reporter:
        raise RuntimeError("Local gh identity unavailable; no cross-repo access assumed")
    print(f"Authenticated GitHub identity: {reporter}", flush=True)
    count = 0
    attempts = 0
    had_failure = False
    for repo, number in search_issues(gh, owner):
        issue = gh_json(gh, f"repos/{repo}/issues/{number}")
        comments = gh_json(gh, f"repos/{repo}/issues/{number}/comments?per_page=100")
        if len(comments) >= 100:
            print(f"skip {repo}#{number}: comment pagination required")
            continue
        if not has_new_request(issue, comments, owner, reporter):
            continue
        _, digest = report_input(issue, comments, owner)
        if already_reviewed(comments, digest, reporter):
            continue
        print(f"candidate {repo}#{number}", flush=True)
        if dry_run:
            count += 1
            continue
        if attempts >= max_reviews + 1:
            print("review attempt budget reached", flush=True)
            break
        attempts += 1
        args = [
            sys.executable, "plugin/scripts/claude_issue_review.py",
            "--repo", repo, "--issue", str(number), "--owner", owner,
            "--reporter", reporter, "--gh", gh, "--claude", claude,
            "--timeout", "330",
        ]
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=410)
        except subprocess.TimeoutExpired:
            print(f"review timed out for {repo}#{number}", file=sys.stderr)
            had_failure = True
            continue
        if result.returncode:
            print(f"review failed for {repo}#{number}: {result.stderr[-1200:]}", file=sys.stderr)
            had_failure = True
            continue
        print(result.stdout.strip(), flush=True)
        if not result.stdout.strip().startswith("reviewed Issue #"):
            continue
        count += 1
        if count >= max_reviews:
            break
    print(f"scan complete: {count} posted (dry_run={dry_run})", flush=True)
    return 1 if had_failure else 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--owner", required=True)
    p.add_argument("--gh", default="gh")
    p.add_argument("--claude", default="claude")
    p.add_argument("--max-reviews", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not 1 <= args.max_reviews <= 2:
        p.error("max-reviews must be 1 or 2")
    raise SystemExit(scan(args.owner, args.gh, args.claude, args.max_reviews, args.dry_run))


if __name__ == "__main__":
    main()
