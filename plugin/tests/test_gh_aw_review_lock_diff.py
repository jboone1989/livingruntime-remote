"""Review generated GH-AW locks through strict CI, not the bounded LLM source diff."""

import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/eventgraph-glm-review.yml"


class GhAwReviewDiffTests(unittest.TestCase):
    def test_both_review_diffs_exclude_only_generated_locks(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("review_paths=(. ':(exclude).github/workflows/*.lock.yml')", source)
        self.assertEqual(source.count(' -- "${review_paths[@]}" >'), 2)
        self.assertIn('test -s "$GITHUB_WORKSPACE/pr-review.diff"', source)
        self.assertIn('> 80000', source)

    def test_git_pathspec_retains_authored_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            lock = workflows / "eventgraph-pi-worker.lock.yml"
            source = workflows / "eventgraph-pi-worker.md"
            other = workflows / "eventgraph-dispatcher.yml"
            for path in (lock, source, other):
                path.write_text("before\n", encoding="utf-8")

            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout

            git("init", "-q")
            git("add", ".")
            git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", "base")
            for path in (lock, source, other):
                path.write_text("after\n", encoding="utf-8")
            diff = git("diff", "--no-ext-diff", "--unified=0", "HEAD", "--", ".", ":(exclude).github/workflows/*.lock.yml")
            self.assertNotIn("eventgraph-pi-worker.lock.yml", diff)
            self.assertIn("eventgraph-pi-worker.md", diff)
            self.assertIn("eventgraph-dispatcher.yml", diff)


if __name__ == "__main__":
    unittest.main()
