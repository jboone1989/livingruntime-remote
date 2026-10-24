"""Cost-free regressions for GitHub Issue Devil's Advocate routing."""
import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "claude_issue_review.py"
spec = importlib.util.spec_from_file_location("claude_issue_review", MODULE)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class IssueReviewTests(unittest.TestCase):
    def test_duplicate_activation_comment_does_not_rebill(self):
        issue = {"title": "Challenge a plan", "body": "Original user goal", "state": "open"}
        comments = [{"user": {"login": "jboone1989"}, "body": "/devils-advocate"}]
        _, a = review.report_input(issue, [], "jboone1989")
        _, b = review.report_input(issue, comments, "jboone1989")
        self.assertEqual(a, b)

    def test_changed_owner_context_requires_fresh_review(self):
        issue = {"title": "Challenge", "body": "Goal"}
        _, a = review.report_input(issue, [], "jboone1989")
        _, b = review.report_input(issue, [{"user": {"login": "jboone1989"}, "body": "New fact"}], "jboone1989")
        self.assertNotEqual(a, b)

    def test_refuses_pr_and_unsafe_pagination(self):
        with self.assertRaises(ValueError):
            review.report_input({"title": "PR", "pull_request": {}}, [], "owner")
        with self.assertRaises(ValueError):
            review.report_input({"title": "Issue"}, [{}] * 100, "owner")

    def test_truncated_tail_changes_digest_and_newest_comment_is_visible(self):
        issue = {"title": "Plan", "body": "A" * 18000}
        older = [{"user": {"login": "owner"}, "body": "OLD" * 4000}]
        initial_context, initial = review.report_input(issue, older, "owner")
        later = older + [{"user": {"login": "owner"}, "body": "new-most-recent-correction"}]
        context, digest = review.report_input(issue, later, "owner")
        self.assertNotEqual(digest, initial)
        self.assertIn("new-most-recent-correction", context)
        edited = dict(issue, body=issue["body"][:17000] + "DIFFERENT" + issue["body"][17009:])
        self.assertNotEqual(digest, review.report_input(edited, later, "owner")[1])

    def test_null_issue_comment_author_is_ignored(self):
        issue = {"title": "Plan", "body": "Goal"}
        comments = [{"user": None, "body": "orphaned comment"}]
        _, digest = review.report_input(issue, comments, "owner")
        self.assertFalse(review.already_reviewed(comments, digest))

    def test_untrusted_user_cannot_forge_dedup_marker(self):
        issue = {"title": "Plan", "body": "Goal"}
        _, digest = review.report_input(issue, [], "owner")
        fake = [{"user": {"login": "attacker"}, "body": f"{review.MARKER}{digest} -->"}]
        self.assertFalse(review.already_reviewed(fake, digest))

    def test_no_local_file_or_shell_tools_exposed_to_issue(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        fake = SimpleNamespace(returncode=0, stdout='{"result":"One useful objection"}', stderr="")
        with patch.object(review.subprocess, "run", return_value=fake) as run:
            answer = review.invoke_claude("claude", Path("/tmp"), "Proposal", 60)
        argv = run.call_args.args[0]
        self.assertEqual(answer, "One useful objection")
        self.assertIn("--agent", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertIn("Read", argv[argv.index("--disallowedTools") + 1])

    def test_bot_output_not_used_as_new_input(self):
        issue = {"title": "Challenge", "body": "Goal"}
        _, digest = review.report_input(issue, [], "owner")
        comments = [{"user": {"login": "github-actions[bot]"}, "body": f"{review.MARKER}{digest} -->\nreport"}]
        _, digest2 = review.report_input(issue, comments, "owner")
        self.assertEqual(digest, digest2)
        self.assertTrue(review.already_reviewed(comments, digest))


if __name__ == "__main__":
    unittest.main()
