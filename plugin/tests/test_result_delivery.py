from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import relay_agent
from result_outbox import ResultOutbox, connector_lock
import execution_status


class ResultDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "outbox.sqlite3"
        self.outbox = ResultOutbox(self.path, "device-a")
        self.cfg = {"url": "https://example.test", "device_token": "test"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_transient_upload_retries_result_without_reexecuting_command(self):
        task = {"task_id": "task-1"}
        with patch.object(relay_agent, "_dispatch", return_value={"ok": True, "result": {"x": 1}}) as dispatch:
            relay_agent._handle_claimed_task(task, self.outbox, threading.Event())
            with patch.object(relay_agent, "_request", side_effect=TimeoutError):
                relay_agent._deliver_results(self.cfg, self.outbox)
            # Simulate restart after the completed result was persisted.
            restored = ResultOutbox(self.path, "device-a")
            restored.recover_interrupted()
            with restored.db() as db:
                db.execute("UPDATE results SET retry_at=0")
            with patch.object(relay_agent, "_request", return_value={"ok": True}) as upload:
                relay_agent._deliver_results(self.cfg, restored)
            relay_agent._handle_claimed_task(task, restored, threading.Event())
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(upload.call_args.args[2]["result"]["result"], {"x": 1})
        self.assertEqual(restored.due(), [])

    def test_missing_ack_keeps_payload_and_repairing_does_not_send_old_owner_results(self):
        self.outbox.begin("task-1")
        self.outbox.finish("task-1", {"ok": True})
        with patch.object(relay_agent, "_request", return_value={}):
            relay_agent._deliver_results(self.cfg, self.outbox)
        with self.outbox.db() as db:
            row = db.execute("SELECT * FROM results").fetchone()
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(ResultOutbox(self.path, "other-device").due(now=1e20), [])

    def test_crash_during_execution_reports_unknown_without_replay(self):
        self.outbox.begin("task-1")
        restored = ResultOutbox(self.path, "device-a")
        restored.recover_interrupted()
        result = json.loads(restored.due()[0]["payload"])
        self.assertFalse(result["ok"])
        self.assertIn("outcome unknown", result["error"])
        self.assertFalse(restored.begin("task-1"))

    def test_second_connector_cannot_recover_a_live_execution(self):
        lock = Path(self.tmp.name) / "connector.lock"
        with connector_lock(lock):
            with self.assertRaises(RuntimeError):
                with connector_lock(lock):
                    self.fail("second connector acquired lock")
        with connector_lock(lock):
            pass

    def test_unknown_or_stale_receipt_never_claims_live_worker(self):
        with patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_EXECUTION_STATE": str(Path(self.tmp.name) / "state.json")}):
            for runtime in [
                {"worker_alive": True, "observed_at": 10},
                {"worker_alive": True, "observed_at": 999, "reconcile_error": "SSH timeout"},
                {"worker_alive": True},
            ]:
                snap = execution_status.snapshot(jobs=[{"job_id": "x", "status": "RUNNING", "runtime": runtime}], now=1000)
                self.assertEqual(snap["state"], "UNKNOWN")
                self.assertEqual(snap["worker_alive_count"], 0)
                self.assertIsNone(snap["active_jobs"][0]["worker_alive"])

    def test_worker_count_is_not_limited_by_display_rows(self):
        with patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_EXECUTION_STATE": str(Path(self.tmp.name) / "state.json")}):
            jobs = [{"status": "WAITING", "runtime": {}} for _ in range(8)]
            jobs.append({"status": "RUNNING", "runtime": {"worker_alive": True, "observed_at": 999}})
            snap = execution_status.snapshot(jobs=jobs, now=1000)
        self.assertEqual(snap["worker_alive_count"], 1)
        self.assertEqual(snap["state"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
