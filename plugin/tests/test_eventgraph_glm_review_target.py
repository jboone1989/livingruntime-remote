"""Focused regressions for central EventGraph GLM review target validation."""

import json
import re
import subprocess
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/eventgraph-glm-review.yml"
REPO = "jboone1989/EventGraph"
SHA = "a" * 40


def extract_jq(source, marker):
    match = re.search(re.escape(marker) + r"\s*'([\s\S]*?)'(?=\s*(?:<<<|\)))", source)
    if not match:
        raise AssertionError(f"Unable to find reviewer jq expression: {marker}")
    return match.group(1)


def pr(base="develop", sha=SHA, repo=REPO, draft=False, state="open", number=4):
    return {
        "number": number, "state": state, "draft": draft,
        "base": {"ref": base, "repo": {"full_name": REPO}},
        "head": {"sha": sha, "repo": {"full_name": repo}},
    }


def execute_jq(expr, payload):
    return subprocess.run(
        ["jq", "-e", "--arg", "sha", SHA, "--arg", "repo", REPO, expr],
        input=json.dumps(payload), capture_output=True, text=True, check=False,
    )


class ReviewerTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.guard = extract_jq(
            cls.source, 'if ! jq -e --arg sha "$CHECK_SHA" --arg repo "$GITHUB_REPOSITORY"'
        )
        cls.selector = extract_jq(
            cls.source, '| jq -r --arg sha "$CHECK_SHA" --arg repo "$GITHUB_REPOSITORY"'
        )

    def test_main_and_develop_accepted(self):
        for base in ("main", "develop"):
            with self.subTest(base=base):
                self.assertEqual(execute_jq(self.guard, pr(base=base)).returncode, 0)
                self.assertEqual(execute_jq(self.selector, [pr(base=base)]).stdout.strip(), "4")

    def test_stale_draft_fork_wrong_base_closed_are_rejected(self):
        for item in (
            pr(sha="b" * 40), pr(draft=True), pr(repo="elsewhere/fork"),
            pr(base="release"), pr(state="closed"),
        ):
            with self.subTest(item=item):
                self.assertNotEqual(execute_jq(self.guard, item).returncode, 0)
                self.assertEqual(execute_jq(self.selector, [item]).stdout.strip(), "")

    def test_ambiguous_exact_sha_rejected(self):
        result = execute_jq(self.selector, [pr(number=4), pr(base="main", number=5)])
        self.assertEqual(result.stdout.strip(), "")

    def test_diff_and_prompt_follow_validated_base(self):
        self.assertIn('base_ref="$(jq -r', self.source)
        self.assertIn('[[ "$base_ref" == develop || "$base_ref" == main ]]', self.source)
        self.assertIn('"refs/heads/$base_ref:refs/remotes/origin/$base_ref"', self.source)
        self.assertIn("against " + "$" + "{{ steps.target.outputs.base_ref }}.", self.source)


if __name__ == "__main__":
    unittest.main()
