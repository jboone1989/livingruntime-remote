from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

os.environ.setdefault(
    "LIVINGRUNTIME_REMOTE_CONFIG",
    str(Path(__file__).resolve().parent / "fixtures" / "missing-config.json"),
)

import bridge  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class UnifiedDiffTests(unittest.TestCase):
    def test_numbered_hunk_replaces_line(self) -> None:
        original = "alpha\nold\nomega\n"
        patch = """--- a/file.py
+++ b/file.py
@@ -1,3 +1,3 @@
 alpha
-old
+new
 omega
"""
        self.assertEqual(bridge.apply_unified_diff(original, patch), "alpha\nnew\nomega\n")

    def test_unnumbered_hunk_finds_unique_block(self) -> None:
        original = "keep\nold\nkeep\n"
        patch = """--- a/file.py
+++ b/file.py
@@
-old
+new
"""
        self.assertEqual(bridge.apply_unified_diff(original, patch), "keep\nnew\nkeep\n")

    def test_mismatch_raises_without_partial_result(self) -> None:
        original = "alpha\nbeta\n"
        patch = """@@ -1,2 +1,2 @@
 alpha
-missing
+new
"""
        with self.assertRaises(ValueError):
            bridge.apply_unified_diff(original, patch)

    def test_ambiguous_hunk_is_rejected(self) -> None:
        original = "old\nmid\nold\n"
        patch = """@@
-old
+new
"""
        with self.assertRaises(ValueError):
            bridge.apply_unified_diff(original, patch)

    def test_empty_patch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            bridge.apply_unified_diff("hello\n", "   \n")


class ApplyPatchToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "remote.json"
        self.config.write_text(
            json.dumps(
                {
                    "ssh_host": "livingruntime-vm",
                    "roots": ["/home/ubuntu"],
                    "systemd_units": [],
                }
            ),
            encoding="utf-8",
        )
        self.original = "def run():\n    return 1\n"
        self.calls: list[tuple[str, dict]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _remote(
        self,
        op: str,
        payload: dict,
        timeout: int = 30,
        project: str | None = None,
        device: str | None = None,
    ) -> dict:
        self.calls.append((op, payload))
        if op == "read_file":
            return {
                "path": payload["path"],
                "offset": 0,
                "bytes_read": len(self.original.encode("utf-8")),
                "content": self.original,
                "sha256": _sha(self.original),
            }
        if op == "write_file":
            self.assertEqual(payload["expected_sha256"], _sha(self.original))
            self.original = payload["content"]
            return {
                "path": payload["path"],
                "mode": payload["mode"],
                "bytes": len(self.original.encode("utf-8")),
                "sha256": _sha(self.original),
            }
        raise AssertionError(op)

    def test_apply_patch_writes_only_after_successful_hunk(self) -> None:
        diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def run():
-    return 1
+    return 2
"""
        with patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(bridge, "_remote", self._remote):
            result = bridge.apply_patch("/home/ubuntu/app.py", diff, expected_sha256=_sha("def run():\n    return 1\n"))
        self.assertTrue(result["changed"])
        self.assertEqual(result["sha256"], _sha("def run():\n    return 2\n"))
        self.assertEqual([op for op, _ in self.calls], ["read_file", "write_file"])

    def test_apply_patch_does_not_write_on_hunk_mismatch(self) -> None:
        diff = """@@ -1,2 +1,2 @@
 def run():
-    return 99
+    return 2
"""
        with patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(bridge, "_remote", self._remote):
            with self.assertRaises(ValueError):
                bridge.apply_patch("/home/ubuntu/app.py", diff)
        self.assertEqual([op for op, _ in self.calls], ["read_file"])

    def test_apply_patch_rejects_stale_hash(self) -> None:
        with patch.object(bridge, "_config_path", return_value=str(self.config)), patch.object(bridge, "_remote", self._remote):
            with self.assertRaises(RuntimeError):
                bridge.apply_patch("/home/ubuntu/app.py", "@@\n-old\n+new\n", expected_sha256="deadbeef")
        self.assertEqual([op for op, _ in self.calls], ["read_file"])


if __name__ == "__main__":
    unittest.main()
