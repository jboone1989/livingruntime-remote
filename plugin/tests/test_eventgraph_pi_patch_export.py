"""Isolated real-Git tests for Pi worktree patch export."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from eventgraph_pi_patch_export import export

PATH = "packages/backend/src/editorial/explain.ts"


class PatchExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.make(PATH, "export const x = 1;\n")
        self.git("add", ".")
        self.git("-c", "user.email=test@example.invalid", "-c",
                 "user.name=Test", "commit", "-qm", "base")
        self.head = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              check=True, capture_output=True, text=True).stdout

    def make(self, path, content):
        item = self.root / path
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text(content, encoding="utf-8")
        return item

    def test_changed_source_and_new_test_are_pinned_to_original_blob(self):
        self.make(PATH, "export const x = 2;\n")
        self.make("tests/explain-fix.test.ts", "assert(true);\n")
        result = export(self.root, 175, self.head)
        self.assertEqual(result["expected_head_sha"], self.head)
        self.assertEqual([r["path"] for r in result["changes"]],
                         [ "packages/backend/src/editorial/explain.ts", "tests/explain-fix.test.ts" ])
        self.assertEqual(result["changes"][0]["expected_blob_sha"],
                         self.git("rev-parse", "HEAD:" + PATH).strip())
        self.assertIsNone(result["changes"][1]["expected_blob_sha"])
        self.assertEqual(result["changes"][0]["content"], "export const x = 2;\n")

    def test_empty_or_unrelated_tree_rejected(self):
        with self.assertRaisesRegex(ValueError, "no changes"):
            export(self.root, 175, self.head)
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            export(self.root, 175, "b" * 40)

    def test_forbidden_path_and_deletion_refused(self):
        self.make("scripts/ci/check-spec.mjs", "malicious\n")
        with self.assertRaises(ValueError):
            export(self.root, 175, self.head)
        (self.root / "scripts/ci/check-spec.mjs").unlink()
        (self.root / PATH).unlink()
        with self.assertRaisesRegex(ValueError, "missing, deleted"):
            export(self.root, 175, self.head)

    def test_symlinks_and_binary_files_not_exported(self):
        target = self.root / PATH
        target.unlink()
        target.symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "symbolic"):
            export(self.root, 175, self.head)
        target.unlink()
        target.write_bytes(b"a\\x00b".replace(b"\\x00", b"\x00"))
        with self.assertRaisesRegex(ValueError, "binary"):
            export(self.root, 175, self.head)

    def test_committed_child_changes_are_exported_but_not_unrelated_history(self):
        self.make(PATH, "export const x = 4;\n")
        self.git("add", PATH)
        self.git("-c", "user.email=test@example.invalid", "-c",
                 "user.name=Test", "commit", "-qm", "local Pi patch")
        self.assertEqual(export(self.root, 175, self.head)["changes"][0]["content"],
                         "export const x = 4;\n")


if __name__ == "__main__":
    unittest.main()
