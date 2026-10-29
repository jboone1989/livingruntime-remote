"""Tests for the EventGraph -> cn-dev native Pi handoff boundary."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eventgraph_pi_repair_dispatch as router

SHA = "a" * 40
PR = {
    "number": 175, "state": "open", "draft": False,
    "base": {"ref": "develop"},
    "head": {"sha": SHA, "repo": {"full_name": router.REPOSITORY}},
}


class PiRepairDispatchTests(unittest.TestCase):
    def test_check_requires_real_failed_run_at_exact_sha(self):
        good = {"head_sha": SHA, "event": "pull_request",
                "conclusion": "failure", "name": "Check"}
        with patch.object(router, "api", return_value=good):
            router.validate_failure(PR, SHA, "check", 123)
            for field, value in [
                ("head_sha", "b" * 40), ("conclusion", "success"),
                ("name", "Other"), ("event", "push"),
            ]:
                with patch.object(router, "api", return_value={**good, field: value}):
                    with self.assertRaisesRegex(ValueError, "not a verified failure"):
                        router.validate_failure(PR, SHA, "check", 123)

    def test_check_run_must_belong_to_current_pr_when_associations_exist(self):
        good = {"head_sha": SHA, "event": "pull_request", "conclusion": "failure",
                "name": "Check", "head_repository": {"full_name": router.REPOSITORY},
                "pull_requests": [{"number": 175}]}
        with patch.object(router, "api", return_value=good):
            router.validate_failure(PR, SHA, "check", 123)
        for delta in [
            {"pull_requests": [{"number": 176}]},
            {"head_repository": {"full_name": "attacker/other"}},
        ]:
            with patch.object(router, "api", return_value={**good, **delta}):
                with self.assertRaisesRegex(ValueError, "not a verified failure"):
                    router.validate_failure(PR, SHA, "check", 123)
        # GitHub sometimes omits workflow_run.pull_requests; exact SHA and
        # repository checks still apply without inventing an association.
        with patch.object(router, "api", return_value={**good, "pull_requests": []}):
            router.validate_failure(PR, SHA, "check", 123)

    def test_only_blocking_claude_failure_routes_to_code_repair(self):
        with patch.object(router, "api", return_value={
            "statuses": [{"context": "claude-review", "state": "failure"}],
        }):
            router.validate_failure(PR, SHA, "claude-review", None)
        # Reviewer/model infrastructure errors are not business-code defects.
        for state in ("success", "pending", "error"):
            with self.subTest(state=state), patch.object(router, "api", return_value={
                "statuses": [{"context": "claude-review", "state": state}],
            }):
                with self.assertRaisesRegex(ValueError, "not blocking"):
                    router.validate_failure(PR, SHA, "claude-review", None)

    def test_reject_wrong_head_base_forks_and_drafts(self):
        for delta in [
            {"head": {"sha": "b" * 40, "repo": PR["head"]["repo"]}},
            {"base": {"ref": "main"}},
            {"draft": True}, {"state": "closed"},
            {"head": {"sha": SHA, "repo": {"full_name": "attacker/fork"}}},
        ]:
            with self.assertRaisesRegex(ValueError, "ineligible"):
                router.validate_failure({**PR, **delta}, SHA, "check", 10)

    def test_untrusted_or_quoted_markers_cannot_exhaust_repair_rounds(self):
        def comment(sha, *, association="NONE", login="attacker", quoted=False):
            marker = (f"<!-- eventgraph-pi-repair sha={sha} "
                      "attempt=1 state=dispatched -->")
            return {
                "body": ("Quoted marker: " if quoted else "") + marker,
                "author_association": association,
                "user": {"login": login},
            }

        comments = [
            comment(SHA),  # A public PR commenter cannot block real repairs.
            comment("b" * 40, association="OWNER", login="jboone1989", quoted=True),
            comment("c" * 40, association="OWNER", login="jboone1989"),
            comment("d" * 40, login="github-actions[bot]"),
            {"body": comment("e" * 40)["body"]},  # Deleted/unknown author.
        ]
        with patch.object(router, "api", return_value=comments):
            markers = router.read_markers(175)
        self.assertEqual(markers, [
            ("c" * 40, 1, "dispatched"), ("d" * 40, 1, "dispatched"),
        ])
        self.assertEqual(router.next_attempt(markers, SHA), 3)

    def test_repair_rounds_are_deduplicated_and_bounded(self):
        markers = [(SHA, 1, "dispatched")]
        self.assertIsNone(router.next_attempt(markers, SHA))
        self.assertEqual(router.next_attempt(markers, "b" * 40), 2)
        self.assertEqual(router.next_attempt(markers + [
            ("b" * 40, 2, "dispatched"), ("c" * 40, 3, "started"),
        ], "d" * 40), 0)
        self.assertEqual(router.next_attempt([(SHA, 1, "worker-unavailable")], SHA), 1)

    def test_success_receipt_does_not_claim_fix_or_green_checks(self):
        calls = []
        def fake_api(path, *, method="GET", body=None):
            calls.append((path, method, body))
            if path.endswith("/pulls/175"):
                return PR
            if method == "POST":
                return {"id": 55}
            return {}
        with patch.object(router, "api", side_effect=fake_api), \
             patch.object(router, "validate_failure"), \
             patch.object(router, "read_markers", return_value=[]), \
             patch.object(router, "dispatch", return_value="lra_123") as dispatched:
            router.run(175, SHA, "check", 1234)
        dispatched.assert_called_once_with(175, SHA, "check", 1234)
        comment = next(body for path, method, body in calls if method == "PATCH")
        self.assertIn("state=dispatched", comment)
        self.assertIn("NOT a claim", comment)
        self.assertIn("state=dispatched", comment)

    def test_unavailable_connector_is_reported_not_success(self):
        calls = []
        def fake_api(path, *, method="GET", body=None):
            calls.append((path, method, body))
            if path.endswith("/pulls/175"):
                return PR
            if method == "POST":
                return {"id": 55}
            return {}
        with patch.object(router, "api", side_effect=fake_api), \
             patch.object(router, "validate_failure"), \
             patch.object(router, "read_markers", return_value=[]), \
             patch.object(router, "dispatch", side_effect=RuntimeError("Connector offline")):
            with self.assertRaisesRegex(RuntimeError, "Connector offline"):
                router.run(175, SHA, "check", 1234)
        marker = next(body for path, method, body in calls if method == "PATCH")
        self.assertIn("state=worker-unavailable", marker)
        self.assertNotIn("state=dispatched", marker)

    def test_invalid_sha_and_number_never_dispatch(self):
        with patch.object(router, "api") as api:
            for number, sha in [(-1, SHA), (101, "BAD"), (101, "b" * 39)]:
                with self.assertRaises(ValueError):
                    router.run(number, sha, "check", 100)
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
