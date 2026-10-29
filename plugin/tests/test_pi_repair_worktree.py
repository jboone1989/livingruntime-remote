"""Real Git regression for native Pi exact-head worktree setup (no GitHub API)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from livingruntime_adapter_pi import prepare_repair_worktree

CN = "e21c871f-317b-4f3e-95bf-8d4299547a40"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=12).stdout.strip()


class NativePiWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "src.ts").write_text("export const n = 1;\n", encoding="utf-8")
        git(self.repo, "add", "src.ts")
        git(self.repo, "-c", "user.email=dev@example.invalid",
            "-c", "user.name=Dev", "commit", "-qm", "baseline")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        origin = root / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repo), str(origin)],
                       check=True, capture_output=True, timeout=12)
        git(self.repo, "remote", "add", "origin", str(origin))
        self.goal = f"[eventgraph-pi-repair pr=175 sha={self.sha}] Fix the actual code"

    def prepare(self, goal=None, device=CN):
        return prepare_repair_worktree(goal if goal is not None else self.goal,
                                       device=device, project="aihot-dev", workspace=str(self.repo))

    def test_real_git_checkout_matches_original_pr_head_and_is_reusable(self):
        path = Path(self.prepare())
        self.assertEqual(path, self.repo / ".worktrees" / f"pi-repair-175-{self.sha[:12]}")
        self.assertEqual(git(path, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(git(path, "status", "--porcelain"), "")
        self.assertEqual(Path(self.prepare()), path)

    def test_mutated_worktree_is_never_reused_or_silently_reset(self):
        path = Path(self.prepare())
        (path / "src.ts").write_text("modified", encoding="utf-8")
        with self.assertRaisesRegex(PermissionError, "differs"):
            self.prepare()
        self.assertEqual((path / "src.ts").read_text(), "modified")

    def test_unmarked_goal_and_other_device_have_distinct_behavior(self):
        self.assertEqual(self.prepare("Ordinary authorized Pi task"), str(self.repo))
        with self.assertRaisesRegex(PermissionError, "only allowed"):
            self.prepare(device="different-device")

    def test_git_root_is_required_before_launch(self):
        nested = self.repo / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "Git worktree root"):
            prepare_repair_worktree(self.goal, device=CN, project="aihot-dev", workspace=str(nested))


if __name__ == "__main__":
    unittest.main()
