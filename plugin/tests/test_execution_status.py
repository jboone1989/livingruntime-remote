from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import execution_status  # noqa: E402


class ExecutionStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "execution-state.json"
        self.env = patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_EXECUTION_STATE": str(self.state)},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_observability_refresh_does_not_manufacture_activity(self) -> None:
        execution_status.record_tool_event(
            "remote_overview",
            True,
            {"count": 1},
            observed_at=100.0,
        )
        snap = execution_status.snapshot(jobs=[], now=200.0)
        self.assertEqual(snap["state"], "IDLE")
        self.assertIsNone(snap["last_real_activity_at"])
        self.assertIsNone(snap["last_tool"])

    def test_process_list_is_observation_but_terminate_is_real_activity(self) -> None:
        execution_status.record_tool_event(
            "process",
            True,
            {"action": "list"},
            observed_at=100.0,
        )
        observed = execution_status.snapshot(jobs=[], now=101.0)
        self.assertIsNone(observed["last_tool"])

        execution_status.record_tool_event(
            "process",
            True,
            {"action": "terminate", "pid": 123},
            observed_at=102.0,
        )
        changed = execution_status.snapshot(jobs=[], now=103.0)
        self.assertEqual(changed["last_tool"], "process")
        self.assertEqual(changed["state"], "RECENT_ACTIVITY")

    def test_recent_real_tool_is_visible_without_claiming_background_work(self) -> None:
        execution_status.record_tool_event(
            "read_file",
            True,
            {"path": "/home/ubuntu/src/app.py"},
            observed_at=100.0,
        )
        snap = execution_status.snapshot(jobs=[], now=110.0)
        self.assertEqual(snap["state"], "RECENT_ACTIVITY")
        self.assertEqual(snap["last_tool"], "read_file")
        self.assertEqual(snap["last_target"], "/home/ubuntu/src/app.py")
        self.assertEqual(snap["active_job_count"], 0)
        self.assertEqual(snap["worker_alive_count"], 0)

    def test_old_activity_with_no_job_reports_idle_and_stale_ui_warning(self) -> None:
        execution_status.record_tool_event(
            "git",
            True,
            {"repo_path": "/home/ubuntu/src/project"},
            observed_at=100.0,
        )
        snap = execution_status.snapshot(jobs=[], now=300.0)
        self.assertEqual(snap["state"], "IDLE")
        self.assertTrue(snap["ui_may_be_stale"])
        self.assertIn("ChatGPT", snap["ui_warning"])
        self.assertEqual(snap["active_job_count"], 0)

    def test_live_durable_worker_is_source_of_truth_for_running(self) -> None:
        jobs = [{
            "job_id": "lrjob_1",
            "status": "RUNNING",
            "terminal": False,
            "goal": "Run tests",
            "current_step": "pytest",
            "next_action": "inspect result",
            "runtime": {
                "worker_alive": True,
                "child_alive": True,
                "heartbeat_age_seconds": 2.0,
                "progress_age_seconds": 1.0,
            },
        }]
        snap = execution_status.snapshot(jobs=jobs, now=500.0)
        self.assertEqual(snap["state"], "RUNNING_EXECUTION")
        self.assertEqual(snap["active_job_count"], 1)
        self.assertEqual(snap["worker_alive_count"], 1)
        self.assertEqual(snap["active_jobs"][0]["current_step"], "pytest")

    def test_cognition_states_are_distinct_from_execution(self) -> None:
        execution_status.record_tool_event(
            "submit_llm_request",
            True,
            {
                "request_id": "llmreq_1",
                "agent_id": "ferro",
                "status": "PENDING",
            },
            observed_at=100.0,
        )
        waiting = execution_status.snapshot(
            jobs=[],
            cognition_status={
                "request_id": "llmreq_1",
                "agent_id": "ferro",
                "status": "PENDING",
            },
            now=101.0,
        )
        self.assertEqual(waiting["state"], "WAITING_FOR_COGNITION")

        claimed = execution_status.snapshot(
            jobs=[],
            cognition_status={
                "request_id": "llmreq_1",
                "agent_id": "ferro",
                "status": "DISPATCHED",
            },
            now=102.0,
        )
        self.assertEqual(claimed["state"], "CLAIMED_BY_CHATGPT")

        done = execution_status.snapshot(
            jobs=[],
            cognition_status={
                "request_id": "llmreq_1",
                "agent_id": "ferro",
                "status": "COMPLETED",
            },
            now=200.0,
        )
        self.assertEqual(done["state"], "IDLE")
        self.assertIsNone(done["cognition"])


if __name__ == "__main__":
    unittest.main()
