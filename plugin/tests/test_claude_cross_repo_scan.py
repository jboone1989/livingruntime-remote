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

    def test_scan_dry_run_skips_claude(self):
        issue = {"state": "open", "labels": [{"name": "devils-advocate"}]}
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
