"""Regression tests for the owner-approved one-time review path."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_pr_review.py
spec = importlib.util.spec_from_file_location("claude_pr_review_under_test", SCRIPT)
reviewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer)
SHA = "ed2563db25e058f00edafc86ef46d6be830a643d"
REPO = "jboone1989/EventGraph"


class OneTimeReviewTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(repo=REPO, pr_number=142, expected_sha=SHA,
                               manual_rereview_comment_id=6398294019,
                               gh="gh")

    def test_approval_bound_to_owner_pr_sha(self):
        good = {"user": {"login": "jboone1989"},
                "issue_url": "https://api.github.com/repos/jboone1989/EventGraph/issues/142",
                "body": "一次性授权记录 PR #142 " + SHA}
        with patch.object(reviewer, "api", return_value=good):
            self.assertEqual(reviewer.verify_owner_approval(self.args()),
                             REPO + "/issues/comments/6398294019")
        for modified in [
            {**good, "user": {"login": "attacker"}},
            {**good, "issue_url": good["issue_url"].replace("/142", "/143")},
            {**good, "body": good["body"].replace(SHA, "f" * 40)},
        ]:
            with patch.object(reviewer, "api", return_value=modified):
                with self.assertRaises(reviewer.ReviewError):
                    reviewer.verify_owner_approval(self.args())

    def test_after_cap_runs_real_review_automatically_once_per_sha(self):
        pr = {"number": 142, "state": "open", "draft": False,
              "head": {"sha": SHA, "repo": {"full_name": REPO}},
              "base": {"ref": "develop"}}
        old = [("a" * 40), ("b" * 40), ("c" * 40)]
        state = {"version": 1, "prs": {"142": {
            "reviewed": old, "outcomes": {s: True for s in old},
            "limited": [SHA]}}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            with (patch.object(reviewer, "checkout", return_value=(Path(d),
                        "diff --git a/plugin/foo.py b/plugin/foo.py\n"
                        "--- a/plugin/foo.py\n+++ b/plugin/foo.py\n"
                        "@@ -1 +1 @@\n-old\n+new\n", "base")),
                  patch.object(reviewer, "deepseek_review", return_value=[]) as deepseek,
                  patch.object(reviewer, "api", return_value=pr),
                  patch.object(reviewer, "already_commented", return_value=False),
                  patch.object(reviewer, "post_comment") as comment,
                  patch.object(reviewer, "commit_status") as status):
                # A fourth distinct SHA must actually be reviewed, even if the old
                # persistent state recorded it as escalated by the previous worker.
                result = reviewer.review(pr, repo=REPO, gh="gh", 
                    work=Path(d), state=state, state_path=path, timeout=60)
                self.assertIn("blocking=False", result)
                deepseek.assert_called_once()
                comment.assert_called_once()
                self.assertEqual(status.call_args.args[3], "success")
                self.assertIn(SHA, reviewer.load_state(path)["prs"]["142"]["reviewed"])
                self.assertEqual(reviewer.review(pr, repo=REPO, gh="gh", 
                    work=Path(d), state=state, state_path=path, timeout=60),
                    "skip:already reviewed")
                deepseek.assert_called_once()


if __name__ == "__main__":
    unittest.main()
