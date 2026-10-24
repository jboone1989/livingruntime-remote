"""Cheap checks for central cross-repository opt-in proposal reviews."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("claude_cross_repo_scan", SCRIPTS / "claude_cross_repo_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class CrossRepoTest(unittest.TestCase):
    def test_search_deduplicates_and_only_trusts_owner_repo(self):
        samples = {
            "items": [
                {"html_url": "https://github.com/jboone1989/EventGraph/issues/123"},
                {"html_url": "https://github.com/jboone1989/EventGraph/issues/123"},
                {"html_url": "https://github.com/unrelated/Other/issues/5"},
                {"html_url": "https://github.com/jboone1989/stories/pull/9"},
            ],
        }
        with patch.object(scan, "gh_json", return_value=samples) as gh:
            result = scan.search_issues("gh", "jboone1989")
        self.assertEqual(result, [("jboone1989/EventGraph", 123)])
        self.assertEqual(gh.call_count, 2)

    def test_label_enables_initial_review_and_comment_renews_only_explicitly(self):
        base = {"state": "open", "labels": [{"name": "devils-advocate"}]}
        self.assertTrue(scan.has_new_request(base, [], "owner", "owner"))
        report = {"user": {"login": "owner"}, "created_at": "2026-10-24T12:00:00Z",
                  "body": f"{scan.MARKER}hash -->\nAlready reviewed"}
        casual = {"user": {"login": "owner"}, "created_at": "2026-10-24T12:05:00Z",
                  "body": "Thanks for checking"}
        self.assertFalse(scan.has_new_request(base, [report, casual], "owner", "owner"))
        command = {"user": {"login": "owner"}, "created_at": "2026-10-24T12:06:00Z",
                   "body": "/devils-advocate"}
        self.assertTrue(scan.has_new_request(base, [report, casual, command], "owner", "owner"))

    def test_untrusted_slash_command_does_not_bill(self):
        issue = {"state": "open", "labels": []}
        comment = {"user": {"login": "stranger"}, "body": "/devils-advocate"}
        self.assertFalse(scan.has_new_request(issue, [comment], "owner", "owner"))

    def test_duplicate_digest_does_not_consume_daily_slot(self):
        from claude_issue_review import report_input, MARKER
        issue = {"title": "Plan", "body": "Original", "state": "open",
                 "labels": [{"name": "devils-advocate"}]}
        _, digest = report_input(issue, [], "owner")
        report = {"user": {"login": "owner"}, "created_at": "2026-10-24T05:00:00Z",
                  "body": f"{MARKER}{digest} -->\nPrevious review"}
        newer_command = {"user": {"login": "owner"}, "created_at": "2026-10-24T05:01:00Z",
                         "body": "/devils-advocate"}
        def get(gh, endpoint):
            if endpoint == "user":
                return {"login": "owner"}
            if "/comments?" in endpoint and endpoint.startswith("repos/owner/Old/"):
                return [report, newer_command]
            if "/comments?" in endpoint:
                return []
            return issue
        with patch.object(scan, "gh_json", side_effect=get), \
             patch.object(scan, "search_issues", return_value=[
                 ("owner/Old", 1), ("owner/New", 2)]), \
             patch.object(scan.subprocess, "run") as run:
            from types import SimpleNamespace
            run.return_value = SimpleNamespace(returncode=0, stdout="reviewed Issue #2: abc", stderr="")
            self.assertEqual(scan.scan("owner", "gh", "claude", 1, False), 0)
            self.assertEqual(run.call_count, 1)
            self.assertIn("owner/New", run.call_args.args[0])

    def test_failed_candidate_does_not_abort_subsequent_review(self):
        issue = {"title": "Plan", "body": "Original", "state": "open",
                 "labels": [{"name": "devils-advocate"}]}
        def get(gh, endpoint):
            if endpoint == "user":
                return {"login": "owner"}
            if "/comments?" in endpoint:
                return []
            return issue
        from types import SimpleNamespace
        with patch.object(scan, "gh_json", side_effect=get), \
             patch.object(scan, "search_issues", return_value=[
                 ("owner/Fail", 1), ("owner/Pass", 2)]), \
             patch.object(scan.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=1, stdout="", stderr="failed"),
                 SimpleNamespace(returncode=0, stdout="reviewed Issue #2: abc", stderr=""),
             ]) as run:
            self.assertEqual(scan.scan("owner", "gh", "claude", 1, False), 1)
            self.assertEqual(run.call_count, 2)

    def test_scan_dry_run_skips_claude(self):
        issue = {"title": "Proposal", "body": "Test the opt-in", "state": "open", "labels": [{"name": "devils-advocate"}]}
        def get(gh, endpoint):
            if endpoint == "user":
                return {"login": "jboone1989"}
            if "/comments?" in endpoint:
                return []
            return issue
        with patch.object(scan, "gh_json", side_effect=get), \
             patch.object(scan, "search_issues", return_value=[("jboone1989/EventGraph", 123)]), \
             patch.object(scan.subprocess, "run") as run:
            self.assertEqual(scan.scan("jboone1989", "gh", "claude", 1, True), 0)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
