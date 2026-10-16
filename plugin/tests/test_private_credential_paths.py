from __future__ import annotations
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bridge

class ProtectedPathsTests(unittest.TestCase):
    def test_private_store_is_inaccessible_via_file_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "credentials"
            store.mkdir()
            target = str(store / "placeholder.txt")
            with patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_CREDENTIALS": str(store)}), \
                 patch.object(bridge, "_config", return_value={}), \
                 patch.object(bridge, "host_for", return_value={"id": "host"}), \
                 patch.object(bridge, "resolve_path", side_effect=lambda cfg,p,**kwargs: p), \
                 patch.object(bridge, "_remote") as execute:
                for operation in (
                    lambda: bridge.read_file(target),
                    lambda: bridge.write_file(target, "test-only"),
                    lambda: bridge.list_dir(str(store)),
                    lambda: bridge.apply_patch(target, "@@ -1 +1 @@\n-a\n+b"),
                ):
                    with self.assertRaises(PermissionError):
                        operation()
                execute.assert_not_called()

    def test_alias_into_store_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "credentials"
            store.mkdir()
            target = store / "placeholder.txt"
            target.write_text("dummy")
            alias = Path(directory) / "alias"
            alias.symlink_to(target)
            with patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_CREDENTIALS": str(store)}):
                with self.assertRaises(PermissionError):
                    bridge._reject_credential_store_path(str(alias))

if __name__ == "__main__":
    unittest.main()
