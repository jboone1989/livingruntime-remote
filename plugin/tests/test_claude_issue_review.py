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

    def test_bot_output_not_used_as_new_input(self):
        issue = {"title": "Challenge", "body": "Goal"}
        _, digest = review.report_input(issue, [], "owner")
        comments = [{"user": {"login": "owner"}, "body": f"{review.MARKER}{digest} -->\nreport"}]
        _, digest2 = review.report_input(issue, comments, "owner")
        self.assertEqual(digest, digest2)
        self.assertTrue(review.already_reviewed(comments, digest))


if __name__ == "__main__":
    unittest.main()
