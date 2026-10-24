"""Regressions for packaged PyInstaller Connector native CLI dispatch."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import connector
import codex_worker
import livingruntime_adapter_claude
from livingruntime_adapter_codex import CodexAdapter


class FrozenAdapterTests(unittest.TestCase):
    def test_codex_worker_dispatch_is_owner_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "jobs"
            root.mkdir()
            sid = str(uuid.uuid4())
            record = root / (sid + ".json")
            record.write_text(json.dumps({"session_id": sid}), encoding="utf-8")
            with patch.dict(os.environ, {"LIVINGRUNTIME_CODEX_JOBS": str(root)}), \
                 patch.object(codex_worker, "main") as main:
                connector.main(["agent-worker", "codex", str(record)])
                main.assert_called_once_with(str(record))
                wrong = Path(td) / (sid + ".json")
                wrong.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(PermissionError, "session root"):
                    connector.main(["agent-worker", "codex", str(wrong)])

    def test_frozen_adapter_spawns_private_worker_subcommand(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "codex"
            fake.touch()
            fake.chmod(0o700)
            jobs = Path(td) / "jobs"
            with patch.dict(os.environ, {"LIVINGRUNTIME_CODEX_JOBS": str(jobs)}):
                adapter = CodexAdapter(str(fake))
                sid = str(uuid.uuid4())
                adapter._write(sid, {"session_id": sid, "status": "PENDING",
                                     "cwd": td, "options": {}})
                with patch.object(livingruntime_adapter_claude.sys, "frozen", True, create=True), \
                     patch.object(livingruntime_adapter_claude.sys, "executable", "/tmp/frozen-connector"), \
                     patch.object(livingruntime_adapter_claude.subprocess, "Popen") as popen:
                    popen.return_value.pid = 24680
                    self.assertEqual(adapter._spawn(sid, "probe", False), sid)
                args = popen.call_args.args[0]
                self.assertEqual(args, ["/tmp/frozen-connector", "agent-worker", "codex",
                                        str(jobs / (sid + ".json"))])


if __name__ == "__main__":
    unittest.main()
