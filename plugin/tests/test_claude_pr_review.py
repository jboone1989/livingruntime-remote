import argparse
import json
import importlib.util
import subprocess
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
    def test_unknown_source_types_cannot_be_misclassified_as_docs(self):
        for path in ["src/main.go", "src/thing.rs", "frontend/app.vue",
                     "web/styles.css", "index.html", "infra/main.tf",
                     "build/unknown", "docs/runtime.sh"]:
            diff = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
            self.assertFalse(reviewer.documentation_only(diff), path)
        self.assertFalse(reviewer.documentation_only("diff without a parseable path"))

    def test_trusted_agent_instructions_still_get_deep_review(self):
        for path in ("docs/claude-review-checklist.md", "plugin/skills/deploy/SKILL.md",
                     "AGENTS.md", "plugin/README.md", "docs/AGENTS.md",
                     "docs/CLAUDE.md", "specs/a/AGENTS.md", "specs/a/SKILL.md"):
            patch = (f"diff --git a/{path} b/{path}\n"
                     f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n")
            self.assertFalse(reviewer.documentation_only(patch), path)

    def test_prose_mixed_with_deletion_rename_or_mode_always_gets_code_review(self):
        doc = ("diff --git a/README.md b/README.md\n"
               "--- a/README.md\n+++ b/README.md\n"
               "@@ -1 +1 @@\n-before\n+after\n")
        deletion = ("diff --git a/src/auth.py b/src/auth.py\n"
                    "deleted file mode 100644\n"
                    "--- a/src/auth.py\n+++ /dev/null\n"
                    "@@ -1 +0,0 @@\n-secret\n")
        rename = ("diff --git a/src/a.go b/src/b.go\n"
                  "similarity index 100%\nrename from src/a.go\nrename to src/b.go\n")
        chmod = ("diff --git a/script.sh b/script.sh\n"
                 "old mode 100644\nnew mode 100755\n")
        for change in (deletion, rename, chmod):
            self.assertFalse(reviewer.documentation_only(doc + change))
            self.assertFalse(reviewer.documentation_only(change + doc))

    def test_prompt_markdown_is_not_a_documentation_review(self):
        for path in ("prompts/system.md", "config/prompts/instruction.md",
                     "industry/prompts/news-explainer.md"):
            patch = (f"diff --git a/{path} b/{path}\n"
                     f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n")
            self.assertFalse(reviewer.documentation_only(patch))

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

    def test_new_sha_after_three_blocked_reviews_is_really_reviewed(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "a"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}, "body": "approved spec"}
        state = {"prs": {"2": {"reviewed": ["s1", "s2", "s3"],
                                "outcomes": {"s1": True, "s2": True, "s3": True},
                                "limited": ["a"*40], "failures": {"a"*40: 2}}}}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "b"*40)) as checkout,
            patch.object(reviewer, "model_review", return_value=[]) as model,
            patch.object(reviewer, "api", return_value=pr),
            patch.object(reviewer, "already_commented", return_value=True),
            patch.object(reviewer, "commit_status") as status,
            patch.object(reviewer, "save_state"),
        ):
            result = reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                                     state=state, state_path=Path("/tmp/unused"), timeout=5)
            self.assertIn("reviewed:", result)
            checkout.assert_called_once()
            model.assert_called_once()
            self.assertEqual(status.call_args.args[3], "success")
            self.assertIn("a"*40, state["prs"]["2"]["reviewed"])
            self.assertEqual(
                reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                                state=state, state_path=Path("/tmp/unused"), timeout=5),
                "skip:already reviewed")
            checkout.assert_called_once()

    def test_external_fork_skipped(self):
        pr = {"number": 1, "state": "open", "draft": False,
              "head": {"sha": "a"*40, "repo": {"full_name": "unknown/repo"}}}
        self.assertEqual(reviewer.review(
            pr, repo="org/repo", gh="gh", 
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

    def test_document_only_uses_configured_model(self):
        document_diff = ("diff --git a/specs/008/spec.md b/specs/008/spec.md\n"
                         "--- a/specs/008/spec.md\n+++ b/specs/008/spec.md\n"
                         "@@ -1 +1 @@\n-old\n+new acceptance criterion\n")
        pr = {"number": 19, "state": "open", "draft": False,
              "head": {"sha": "a"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "develop"}, "body": "document the approved deployment"}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), document_diff, "b"*40)),
            patch.object(reviewer, "model_review", return_value=[]) as model,
            patch.object(reviewer, "api", return_value=pr),
            patch.object(reviewer, "already_commented", return_value=True),
            patch.object(reviewer, "commit_status") as status,
            patch.object(reviewer, "save_state"),
        ):
            reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                            state={"prs": {}}, state_path=Path("/tmp/unused"), timeout=360)
            model.assert_called_once_with(Path("/tmp"), document_diff, pr["body"], 360)
            self.assertEqual(status.call_args.args[3], "success")

    def test_document_model_failure_retries_full_checklist(self):
        doc = ("diff --git a/specs/x/spec.md b/specs/x/spec.md\n"
               "--- a/specs/x/spec.md\n+++ b/specs/x/spec.md\n"
               "@@ -1 +1 @@\n-old\n+new\n")
        with patch.object(reviewer, "call_model",
                          side_effect=[reviewer.ReviewError("invalid JSON"), []]) as call:
            self.assertEqual(reviewer.model_review(Path("/tmp"), doc, "approved document", 360), [])
            self.assertEqual(call.call_count, 2)
            self.assertIn("documentation-only", call.call_args_list[0].args[0])
            self.assertIn("TRUSTED REVIEW CHECKLIST", call.call_args_list[1].args[0])

    def test_model_settings_are_provider_neutral(self):
        with patch.dict(reviewer.os.environ, {
            "REVIEW_BASE_URL": "https://gateway.example/v1",
            "REVIEW_API_KEY": "test-key",
            "REVIEW_MODEL": "model-id",
        }, clear=True):
            self.assertEqual(reviewer.model_settings(),
                             ("https://gateway.example/v1", "test-key", "model-id"))
        with patch.dict(reviewer.os.environ, {}, clear=True):
            with self.assertRaises(reviewer.ReviewUnavailable):
                reviewer.model_settings()

    def test_openai_compatible_request_uses_configured_url_key_and_model(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({"choices": [{"message": {
                    "content": json.dumps({"findings": []})
                }}]}).encode()
        with patch.dict(reviewer.os.environ, {
            "REVIEW_BASE_URL": "https://gateway.example/v1",
            "REVIEW_API_KEY": "secret-key",
            "REVIEW_MODEL": "other-model",
        }, clear=True), patch.object(reviewer.urllib.request, "urlopen",
                                     return_value=Response()) as request:
            self.assertEqual(reviewer.call_model("review prompt", 30), [])
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, "https://gateway.example/v1/chat/completions")
        self.assertEqual(req.headers["Authorization"], "Bearer secret-key")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "other-model")
        self.assertEqual(body["messages"][1]["content"], "review prompt")

    def test_model_response_parsing(self):
        self.assertEqual(reviewer.parse_model_findings(
            '{"findings":[]}'), [])
        with self.assertRaises(reviewer.ReviewError):
            reviewer.parse_model_findings("not json")

    def test_model_review_verifies_candidates_with_the_same_provider(self):
        candidate = {"path": "src/a.mjs", "line": 4, "confidence": 96,
                     "severity": "important", "reason": "silenced error",
                     "evidence": "catch returns success", "fix": "propagate error"}
        with patch.object(reviewer, "call_model",
                          side_effect=[[candidate], [candidate]]) as call:
            findings = reviewer.model_review(Path("/tmp"), DIFF, "approved spec", 60)
        self.assertEqual(findings, [candidate])
        self.assertEqual(call.call_count, 2)
        self.assertIn("TRUSTED REVIEW CHECKLIST", call.call_args_list[0].args[0])
        self.assertIn("CANDIDATES", call.call_args_list[1].args[0])

    def test_changed_file_source_context_is_bounded_and_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src" / "a.mjs"
            source.parent.mkdir()
            source.write_text("export const value = 1;", encoding="utf-8")
            context = reviewer.changed_source_context(Path(tmp), DIFF)
            self.assertIn("FILE src/a.mjs", context)
            self.assertIn("export const value = 1;", context)

    def test_quality_prompt_explicitly_challenges_bad_shortcuts(self):
        prompt = reviewer.quality_prompt(DIFF, "tests passed")
        for phrase in ("duplicate implementations", "defensive compatibility",
                       "swallowed errors", "fake production data",
                       "tests changed merely to pass", "disabled checks",
                       "deviations from a previously established Spec"):
            self.assertIn(phrase, prompt)
        self.assertIn("PR BODY (untrusted)", prompt)
        self.assertIn("DIFF (untrusted)", prompt)

    def test_checklist_covers_ferro_line_policy_and_all_review_dimensions(self):
        prompt = reviewer.quality_prompt(DIFF, "author claim")
        for key in range(1, 19):
            self.assertIn(f"R{key:02d}", prompt)
        for phrase in ("50 lines or fewer", "over 100 lines",
                       "do not split merely to reach a number",
                       "Module", "R16", "R17", "swallowed errors",
                       "runtime", "architecture", "unnecessary complexity"):
            self.assertIn(phrase.casefold(), prompt.casefold())
        self.assertLess(prompt.index("TRUSTED REVIEW CHECKLIST"),
                        prompt.index("PR BODY (untrusted)"))
        self.assertIn("\nDIFF (untrusted):\n", prompt)

    def test_quality_review_includes_live_prompts_and_configuration(self):
        def changed(path):
            return (
                "diff --git a/" + path + " b/" + path + "\n"
                "--- a/" + path + "\n"
                "+++ b/" + path + "\n"
                "@@ -1 +1 @@\n"
                "-old\n+new\n"
            )
        for path in ("src/a.py", "industry/prompts/selection-score.md",
                     "prompts/runtime.md", ".github/workflows/check.yml",
                     ".env", "Dockerfile", "config/provider.toml"):
            with self.subTest(path=path):
                self.assertTrue(reviewer.needs_quality_review(changed(path)))
        for path in ("docs/README.md", "specs/demo/spec.md", "README.md"):
            with self.subTest(path=path):
                self.assertFalse(reviewer.needs_quality_review(changed(path)))

    def test_concrete_quality_finding_blocks_merge(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "b"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}, "body": "approved spec"}
        finding = {"path": "src/a.mjs", "line": 4, "confidence": 96,
                   "severity": "important", "reason": "silenced error",
                   "evidence": "catch returns success", "fix": "propagate error"}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)),
            patch.object(reviewer, "model_review", return_value=[finding]) as model,
            patch.object(reviewer, "api", return_value=pr),
            patch.object(reviewer, "already_commented", return_value=True),
            patch.object(reviewer, "commit_status") as status,
            patch.object(reviewer, "save_state"),
        ):
            reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                            state={"prs": {}}, state_path=Path("/tmp/unused"), timeout=5)
            model.assert_called_once()
            self.assertEqual(status.call_args.args[3], "failure")

    def test_clean_review_resets_round_counter(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "b"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}}
        state = {"prs": {"2": {"reviewed": ["s1", "s2", "s3"],
                                "outcomes": {"s1": True, "s2": False, "s3": False}}}}
        with patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)), \
             patch.object(reviewer, "model_review", return_value=[]), \
             patch.object(reviewer, "api", return_value=pr), \
             patch.object(reviewer, "already_commented", return_value=True), \
             patch.object(reviewer, "commit_status"), \
             patch.object(reviewer, "save_state"):
            result = reviewer.review(pr, repo="org/repo", gh="gh", 
                                     work=Path("/tmp"), state=state,
                                     state_path=Path("/tmp/unused"), timeout=5)
            self.assertIn("reviewed:", result)
            self.assertFalse(state["prs"]["2"]["outcomes"]["b"*40])

    def test_checkout_fetches_current_remote_refs_into_bare_cache(self):
        def git(*args):
            return subprocess.run(["git", *map(str, args)], check=True,
                                  capture_output=True, text=True).stdout.strip()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, author, work = root / "remote.git", root / "author", root / "work"
            work.mkdir()
            git("init", "--bare", remote)
            git("init", author)
            git("-C", author, "config", "user.email", "review@example.test")
            git("-C", author, "config", "user.name", "Review Test")
            git("-C", author, "checkout", "-b", "develop")
            (author / "file.txt").write_text("base\n")
            git("-C", author, "add", ".")
            git("-C", author, "commit", "-m", "base")
            git("-C", author, "remote", "add", "origin", remote)
            git("-C", author, "push", "origin", "develop")
            git("-C", author, "checkout", "-b", "change")
            (author / "file.txt").write_text("feature\n")
            git("-C", author, "commit", "-am", "feature")
            head = git("-C", author, "rev-parse", "HEAD")
            git("-C", author, "push", "origin", "HEAD:refs/pull/17/head")
            git("-C", author, "checkout", "develop")
            base_before = git("-C", author, "rev-parse", "HEAD")

            original_run = reviewer.run
            def local_run(args, **kwargs):
                # Keep the production URL validation while routing Git I/O to
                # this test's local bare remote (no network dependency).
                if args[-3:] == ["remote", "get-url", "origin"]:
                    return "https://github.com/org/repo.git\n"
                args = [str(remote) if a == "https://github.com/org/repo.git" else a
                        for a in args]
                return original_run(args, **kwargs)

            cache = work / "pr-17-objects.git"
            with patch.object(reviewer, "run", side_effect=local_run):
                with tempfile.TemporaryDirectory(dir=work) as temporary:
                    source = Path(temporary) / "source"
                    directory, diff, baseline = reviewer.checkout(
                        "org/repo", 17, head, "develop", source, cache)
                    self.assertEqual(directory, source)
                    self.assertEqual(baseline, base_before)
                    self.assertIn("+feature", diff)
                    self.assertEqual((source / "file.txt").read_text(), "feature\n")
                self.assertFalse(source.exists())

                (author / "other.txt").write_text("new base\n")
                git("-C", author, "add", ".")
                git("-C", author, "commit", "-m", "advance develop")
                git("-C", author, "push", "origin", "develop")
                base_after = git("-C", author, "rev-parse", "HEAD")
                with tempfile.TemporaryDirectory(dir=work) as temporary:
                    reviewer.checkout("org/repo", 17, head, "develop",
                                      Path(temporary) / "source", cache)
                self.assertEqual(git("--git-dir", cache, "rev-parse",
                                     "refs/review/base"), base_after)
                self.assertEqual(git("--git-dir", cache, "rev-parse",
                                     "refs/review/head"), head)
                self.assertEqual(git("--git-dir", cache, "for-each-ref",
                                     "--format=%(refname)", "refs/heads"), "")

    def test_temporary_review_checkout_cleaned_when_model_fails(self):
        pr = {"number": 17, "state": "open", "draft": False,
              "head": {"sha": "b"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "develop"}}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "reviews"
            with (
                patch.object(reviewer, "checkout", return_value=(work / "source", DIFF, "a"*40)),
                patch.object(reviewer, "model_review",
                             side_effect=reviewer.ReviewError("Model provider unavailable")),
            ):
                with self.assertRaisesRegex(reviewer.ReviewError, "Model provider unavailable"):
                    reviewer.review(pr, repo="org/repo", gh="gh", work=work,
                                    state={"prs": {}}, state_path=Path(tmp) / "state.json",
                                    timeout=5)
            self.assertEqual(list(work.iterdir()), [])

    def test_commit_status_publishes_deepseek_and_legacy_gate_contexts(self):
        with patch.object(reviewer, "run") as run:
            reviewer.commit_status("gh", "org/repo", "a"*40, "success", "review passed")
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(calls), 3)
        self.assertTrue(any("context=model-review" in args for args in calls))
        self.assertTrue(any("context=deepseek-review" in args for args in calls))
        self.assertTrue(any("context=claude-review" in args for args in calls))

    def test_comment_marker_and_blocking(self):
        finding = {"path": "src/a.mjs", "line": 4,
                   "severity": "important", "confidence": 96,
                   "reason": "race", "evidence": "double post", "fix": "lock"}
        body, blocked = reviewer.format_report("org/repo", "b"*40, 1, [finding])
        self.assertTrue(blocked)
        self.assertIn("claude-review-worker:v1", body)
        self.assertIn("blob/" + "b"*40 + "/src/a.mjs#L4", body)

    def test_dispatched_review_rejects_superseded_ci_head(self):
        with tempfile.TemporaryDirectory() as root:
            pr = {"number": 47, "head": {"sha": "b" * 40}}
            args = argparse.Namespace(repo="org/repo", gh="gh", 
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
            args = argparse.Namespace(repo="org/repo", gh="gh", 
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
            args = argparse.Namespace(repo="org/repo", gh="gh", 
                                      pr_number=47, work=Path(root), timeout=5,
                                      state=Path(root) / "state.json")
            with patch.object(reviewer, "api", return_value=pr) as api, \
                 patch.object(reviewer, "review", return_value="reviewed") as review:
                self.assertFalse(reviewer.run_once(args))
                api.assert_called_once_with("gh", "repos/org/repo/pulls/47")
                review.assert_called_once()

    def test_model_failure_never_falls_back_to_another_provider(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "e"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}, "body": ""}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)),
            patch.object(reviewer, "model_review",
                         side_effect=reviewer.ReviewError("Model provider unavailable")),
        ):
            with self.assertRaisesRegex(reviewer.ReviewError, "Model provider unavailable"):
                reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                                state={"prs": {}}, state_path=Path("/tmp/unused"),
                                timeout=5)

    def test_review_uses_only_the_configured_model_provider(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "b"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}, "body": ""}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)),
            patch.object(reviewer, "model_review", return_value=[]) as model,
            patch.object(reviewer, "api", return_value=pr),
            patch.object(reviewer, "already_commented", return_value=True),
            patch.object(reviewer, "commit_status") as status,
            patch.object(reviewer, "save_state"),
        ):
            result = reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                                     state={"prs": {}}, state_path=Path("/tmp/unused"),
                                     timeout=5)
        self.assertIn("provider=deepseek-flash", result)
        model.assert_called_once()
        self.assertEqual(status.call_args.args[3], "success")

    def test_code_review_uses_the_configured_model_provider(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "b"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}, "body": ""}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)),
            patch.object(reviewer, "model_review", return_value=[]) as model,
            patch.object(reviewer, "api", return_value=pr),
            patch.object(reviewer, "already_commented", return_value=True),
            patch.object(reviewer, "commit_status") as status,
            patch.object(reviewer, "save_state"),
        ):
            result = reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                                     state={"prs": {}}, state_path=Path("/tmp/unused"),
                                     timeout=5)
        self.assertIn("provider=deepseek-flash", result)
        model.assert_called_once()
        self.assertEqual(status.call_args.args[3], "success")

    def test_model_review_does_not_wait_for_an_account_login(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "b"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}, "body": ""}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)),
            patch.object(reviewer, "model_review", return_value=[]) as model,
            patch.object(reviewer, "api", return_value=pr),
            patch.object(reviewer, "already_commented", return_value=False),
            patch.object(reviewer, "post_comment") as comment,
            patch.object(reviewer, "commit_status") as status,
            patch.object(reviewer, "save_state"),
        ):
            result = reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                                     state={"prs": {}}, state_path=Path("/tmp/unused"),
                                     timeout=5)
        self.assertIn("provider=deepseek-flash", result)
        model.assert_called_once()
        self.assertIn("deepseek-flash", comment.call_args.args[3])
        self.assertEqual(status.call_args.args[3], "success")

    def test_each_new_review_sha_uses_the_configured_model(self):
        pr = {"number": 2, "state": "open", "draft": False,
              "head": {"sha": "d"*40, "repo": {"full_name": "org/repo"}},
              "base": {"ref": "main"}, "body": ""}
        with (
            patch.object(reviewer, "checkout", return_value=(Path("/tmp"), DIFF, "a"*40)),
            patch.object(reviewer, "model_review", return_value=[]) as model,
            patch.object(reviewer, "api", return_value=pr),
            patch.object(reviewer, "already_commented", return_value=True),
            patch.object(reviewer, "commit_status"),
            patch.object(reviewer, "save_state"),
        ):
            result = reviewer.review(pr, repo="org/repo", gh="gh", work=Path("/tmp"),
                                     state={"prs": {}}, state_path=Path("/tmp/unused"),
                                     timeout=5)
        self.assertIn("provider=deepseek-flash", result)
        model.assert_called_once()

    def test_quota_without_key_does_not_consume_retry_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            pr = {"number": 7, "state": "open", "draft": False,
                  "head": {"sha": "c"*40, "repo": {"full_name": "org/repo"}},
                  "base": {"ref": "main"}}
            args = argparse.Namespace(repo="org/repo", gh="gh", 
                        pr_number=7, expected_sha=pr["head"]["sha"],
                        work=Path(tmp), timeout=5, state=Path(tmp)/"state")
            with patch.object(reviewer, "api", return_value=pr), \
                 patch.object(reviewer, "review",
                              side_effect=reviewer.ReviewUnavailable("key absent")), \
                 patch.object(reviewer, "commit_status") as status:
                self.assertTrue(reviewer.run_once(args))
            self.assertFalse(args.state.exists())
            self.assertEqual(status.call_args.args[3], "error")

if __name__ == "__main__":
    unittest.main()
