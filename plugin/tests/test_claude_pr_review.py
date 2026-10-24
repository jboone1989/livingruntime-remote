import argparse
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / "plugin" / "scripts" / "claude_pr_review.py"
spec = importlib.util.spec_from_file_location("review_worker", PATH)
reviewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer)

DIFF = """diff --git a/src/a.mjs b/src/a.mjs
--- a/src/a.mjs
+++ b/src/a.mjs
@@ -3,2 +3,3 @@
 unchanged
+added line
 another
"""

class ReviewerUnitTests(unittest.TestCase):
    def test_codex_handoff_requires_opt_in_and_safe_changed_paths(self):
        pr = {"base": {"ref": "develop"}, "labels": [{"name": "agent:autofix"}]}
        diff = """diff --git a/packages/backend/src/admin/notes.ts b/packages/backend/src/admin/notes.ts
--- a/packages/backend/src/admin/notes.ts
+++ b/packages/backend/src/admin/notes.ts
@@ -1,1 +1,2 @@
 original
+changed
"""
        self.assertTrue(reviewer.codex_handoff_eligible(
            pr, "jboone1989/EventGraph", diff, 1, True))
        self.assertFalse(reviewer.codex_handoff_eligible(
            pr, "jboone1989/EventGraph", diff, 3, True))
        self.assertFalse(reviewer.codex_handoff_eligible(
            pr, "jboone1989/EventGraph", diff, 1, False))
        self.assertFalse(reviewer.codex_handoff_eligible(
            {**pr, "labels": []}, "jboone1989/EventGraph", diff, 1, True))
        self.assertFalse(reviewer.codex_handoff_eligible(
            pr, "jboone1989/EventGraph",
            diff.replace("packages/backend/src/admin/notes.ts", ".github/workflows/check.yml"),
            1, True))

    def test_codex_handoff_calls_relay_with_existing_device_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".livingruntime"
            path.mkdir()
            (path / "relay.json").write_text(json.dumps({
                "url": "https://remote.example.test",
                "device_token": "private-credential",
            }))
            class Response:
                def __enter__(self): return self
                def __exit__(self, *_): return None
                def read(self, _): return b'{"ok":true,"session_id":"lra_test"}'
            with patch.object(reviewer.Path, "home", return_value=Path(tmp)), \
                 patch.dict(reviewer.os.environ, {
                     "EVENTGRAPH_CODEX_CONNECTOR_ID": "20501eff-0ef8-42bd-aa5b-283ff5994d52",
                 }), \
                 patch.object(reviewer.urllib.request, "urlopen", return_value=Response()) as send:
                session = reviewer.handoff_to_vm13(11, "a" * 40, "verified review")
                self.assertEqual(session, "lra_test")
                request = send.call_args.args[0]
                self.assertEqual(request.full_url, "https://remote.example.test/device/autofix")
                self.assertEqual(request.get_header("Authorization"), "Bearer private-credential")
                self.assertEqual(json.loads(request.data)["sha"], "a" * 40)

    def test_anchors_only_added_lines(self):
        self.assertEqual(reviewer.changed_lines(DIFF), {"src/a.mjs": {4}})

    def test_filters_confidence_scope_and_duplicates(self):
        def finding(line=4, conf=90, path="src/a.mjs"):
            return {"path": path, "line": line, "confidence": conf,
                    "severity": "important", "reason": "broken",
                    "evidence": "code path", "fix": "use lock"}
        self.assertEqual(reviewer.filter_findings(
            [finding(), finding(), finding(line=5), finding(conf=79),
             finding(path="other")], DIFF), [finding()])

    def test_atomic_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s" / "state.json"
            expected = {"version": 1, "prs": {"5": {"reviewed": ["a"]}}}
            reviewer.save_state(path, expected)
            self.assertEqual(reviewer.load_state(path), expected)
            self.assertEqual(reviewer.load_state(Path(tmp) / "absent"),
                             {"version": 1, "prs": {}})

    def test_round_limit_never_runs_claude(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "a"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}}
        state = {"prs": {"2": {"reviewed": ["s1", "s2", "s3"]}}}
        with patch.object(reviewer, "commit_status") as status, \
             patch.object(reviewer, "checkout") as checkout:
            self.assertEqual(reviewer.review(
                pr, repo="org/repo", gh="gh", claude="claude",
                work=Path("/tmp"), state=state,
                state_path=Path("/tmp/unused"), timeout=5), "limit:manual review")
            status.assert_called_once()
            checkout.assert_not_called()

    def test_external_fork_skipped(self):
        pr = {"number": 1, "state": "open", "draft": False,
              "head": {"sha": "a"*40, "repo": {"full_name": "unknown/repo"}}}
        self.assertEqual(reviewer.review(
            pr, repo="org/repo", gh="gh", claude="claude",
            work=Path("/tmp"), state={"prs": {}},
            state_path=Path("/tmp/unused"), timeout=5), "skip:external fork")


    def test_added_plus_plus_lines(self):
        diff = """diff --git a/src/a.mjs b/src/a.mjs
--- a/src/a.mjs
+++ b/src/a.mjs
@@ -0,0 +1,3 @@
+++i;
+++ b/not-file-header
+normal
"""
        self.assertEqual(reviewer.changed_lines(diff), {"src/a.mjs": {1, 2, 3}})

    def test_claude_prompt_via_stdin_no_project_hooks(self):
        class Result:
            returncode = 0
            stdout = '{"result":"{\\"findings\\":[]}","is_error":false}'
            stderr = ""
        with patch.object(reviewer.subprocess, "run", return_value=Result()) as proc:
            self.assertEqual(reviewer.ask_claude("claude", Path("/tmp"), "prompt", 30), [])
            args = proc.call_args.args[0]
            self.assertEqual(proc.call_args.kwargs["input"], "prompt")
            self.assertNotIn("prompt", args)
            self.assertIn("--setting-sources", args)
            self.assertIn('{"disableAllHooks": true}', args)

    def test_clean_review_resets_round_counter(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "b"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}}
        state = {"prs": {"2": {"reviewed": ["s1", "s2", "s3"],
                                  "outcomes": {"s1": True, "s2": False, "s3": False}}}}
        with patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)), \
             patch.object(reviewer, "native_review", return_value=[]), \
             patch.object(reviewer, "ask_claude", return_value=[]), \
             patch.object(reviewer, "api", return_value=pr), \
             patch.object(reviewer, "already_commented", return_value=True), \
             patch.object(reviewer, "commit_status"), \
             patch.object(reviewer, "save_state"):
            result = reviewer.review(pr, repo="org/repo", gh="gh", claude="claude",
                                     work=Path("/tmp"), state=state,
                                     state_path=Path("/tmp/unused"), timeout=5)
            self.assertIn("reviewed:", result)
            self.assertFalse(state["prs"]["2"]["outcomes"]["b"*40])

    def test_comment_marker_and_blocking(self):
        finding = {"path": "src/a.mjs", "line": 4,
                   "severity": "important", "confidence": 96,
                   "reason": "race", "evidence": "double post", "fix": "lock"}
        body, blocked = reviewer.format_report("org/repo", "b"*40, 1, [finding])
        self.assertTrue(blocked)
        self.assertIn("claude-review-worker:v1", body)
        self.assertIn("blob/" + "b"*40 + "/src/a.mjs#L4", body)

    def test_candidate_response_parsing(self):
        self.assertEqual(reviewer.parse_claude(
            '{"result":"{\\"findings\\":[]}", "is_error":false}'), [])

    def test_native_review_parses_bundled_findings(self):
        native = [{"file": "src/a.mjs", "line": 4, "summary": "Crash",
                   "failure_scenario": "a is None"}]
        output = json.dumps({"subtype": "success", "is_error": False,
                             "result": "```json\n" + json.dumps(native) + "\n```"})
        self.assertEqual(reviewer.parse_native_review(output),
                         [{"path": "src/a.mjs", "line": 4,
                           "reason": "Crash", "evidence": "a is None"}])
        self.assertEqual(reviewer.parse_native_review(json.dumps(
            {"subtype": "success", "is_error": False, "result": "```json\n[]\n```"})), [])

        wrapped = json.dumps({"subtype": "success", "is_error": False,
                              "result": "Reviewing PR diff...\n```json\n" +
                                        json.dumps(native) +
                                        "\n```\nAdditional context."})
        self.assertEqual(reviewer.parse_native_review(wrapped),
                         reviewer.parse_native_review(output))

    def test_native_review_rejects_nonreview_or_failure(self):
        for result in ({"result": "I couldn't run git diff", "is_error": False},
                       {"result": "```json\n{}\n```", "is_error": False},
                       {"result": "[]", "is_error": True}):
            with self.subTest(result=result), self.assertRaises(reviewer.ReviewError):
                reviewer.parse_native_review(json.dumps(result))

    def test_native_invocation_uses_real_review_skill(self):
        with patch.object(reviewer, "run", return_value=json.dumps({
                "subtype": "success", "is_error": False,
                "result": "```json\n[]\n```"})) as cli:
            self.assertEqual(reviewer.native_review("claude", Path("/tmp"), "a"*40, "b"*40, 60), [])
            argv = cli.call_args.args[0]
            self.assertIn("/code-review high " + "a"*40 + ".." + "b"*40, argv)
            self.assertIn("--permission-mode", argv)
            self.assertIn("--disallowedTools", argv)
            self.assertNotIn("--bare", argv)

    def test_dispatched_review_rejects_superseded_ci_head(self):
        with tempfile.TemporaryDirectory() as root:
            pr = {"number": 47, "head": {"sha": "b" * 40}}
            args = argparse.Namespace(repo="org/repo", gh="gh", claude="claude",
                                      pr_number=47, expected_sha="a" * 40,
                                      work=Path(root), timeout=5,
                                      state=Path(root) / "pr-47.json")
            with patch.object(reviewer, "api", return_value=pr), \
                 patch.object(reviewer, "review") as review, \
                 patch.object(reviewer, "commit_status") as status:
                self.assertFalse(reviewer.run_once(args))
                review.assert_not_called()
                status.assert_not_called()

    def test_dispatched_review_accepts_exact_check_sha(self):
        with tempfile.TemporaryDirectory() as root:
            pr = {"number": 47, "head": {"sha": "b" * 40}}
            args = argparse.Namespace(repo="org/repo", gh="gh", claude="claude",
                                      pr_number=47, expected_sha="b" * 40,
                                      work=Path(root), timeout=5,
                                      state=Path(root) / "pr-47.json")
            with patch.object(reviewer, "api", return_value=pr), \
                 patch.object(reviewer, "review", return_value="reviewed") as review:
                self.assertFalse(reviewer.run_once(args))
                review.assert_called_once()

    def test_event_reviews_only_requested_pr(self):
        with tempfile.TemporaryDirectory() as root:
            pr = {"number": 47, "head": {"sha": "a" * 40}}
            args = argparse.Namespace(repo="org/repo", gh="gh", claude="claude",
                                      pr_number=47, work=Path(root), timeout=5,
                                      state=Path(root) / "state.json")
            with patch.object(reviewer, "api", return_value=pr) as api, \
                 patch.object(reviewer, "review", return_value="reviewed") as review:
                self.assertFalse(reviewer.run_once(args))
                api.assert_called_once_with("gh", "repos/org/repo/pulls/47")
                review.assert_called_once()

if __name__ == "__main__":
    unittest.main()
