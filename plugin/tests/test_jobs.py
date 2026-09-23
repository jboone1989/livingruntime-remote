from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import jobs  # noqa: E402


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "jobs"
        self.env = patch.dict(
            os.environ,
            {"LIVINGRUNTIME_REMOTE_JOBS": str(self.root)},
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_create_checkpoint_list_and_terminal_state(self) -> None:
        created = jobs.create(goal="Fix Ferro", project="ferro", device="main")
        self.assertEqual(created["status"], "PENDING")
        self.assertFalse(created["terminal"])

        running = jobs.checkpoint(
            created["job_id"],
            summary="Started diagnosis.",
            current_step="inspect logs",
            next_action="patch service",
            status="RUNNING",
        )
        self.assertEqual(running["status"], "RUNNING")
        self.assertEqual(running["current_step"], "inspect logs")
        self.assertEqual(len(running["checkpoints"]), 1)

        completed = jobs.checkpoint(
            created["job_id"],
            summary="Tests passed.",
            status="SUCCEEDED",
        )
        self.assertTrue(completed["terminal"])
        self.assertEqual(completed["status"], "SUCCEEDED")

        rows = jobs.list_jobs()
        self.assertEqual([row["job_id"] for row in rows], [created["job_id"]])
        self.assertEqual(jobs.list_jobs(status="SUCCEEDED")[0]["job_id"], created["job_id"])

    def test_terminal_job_cannot_reopen(self) -> None:
        created = jobs.create(goal="One way")
        jobs.checkpoint(created["job_id"], summary="done", status="FAILED")
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            jobs.checkpoint(created["job_id"], summary="retry", status="RUNNING")

    def test_backend_job_is_deduplicated(self) -> None:
        first = jobs.ensure_backend_job(
            backend_type="pi",
            backend_job_id="pi-123",
            goal="Long task",
            backend_details={"pi_remote_dir": "/home/ubuntu/src/pi-remote"},
        )
        second = jobs.ensure_backend_job(
            backend_type="pi",
            backend_job_id="pi-123",
            goal="Different text should not duplicate",
        )
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(jobs.list_jobs()), 1)

    def test_backend_status_sync_records_terminal_checkpoint(self) -> None:
        created = jobs.ensure_backend_job(
            backend_type="pi",
            backend_job_id="pi-456",
            goal="Run tests",
        )
        completed = jobs.sync_backend_status(
            created["job_id"],
            backend_status="SUCCEEDED",
            summary="Pi completed successfully.",
        )
        self.assertEqual(completed["status"], "SUCCEEDED")
        self.assertTrue(completed["terminal"])
        self.assertEqual(completed["checkpoints"][-1]["source"], "backend")

    def test_attach_backend_reuses_nonterminal_goal_and_preserves_session_metadata(self) -> None:
        created = jobs.create(goal="Multi-step goal", project="ferro", device="main")
        attached = jobs.attach_backend(
            created["job_id"],
            {
                "type": "pi-step",
                "job_id": "step-1",
                "pi_remote_dir": "/home/ubuntu/src/pi-remote-runtime",
                "job_root": "/home/ubuntu/.livingruntime/pi-jobs",
                "session_file": "/home/ubuntu/.livingruntime/pi-sessions/session.jsonl",
                "session_id": "session-1",
                "controller_mode": "external",
            },
        )
        self.assertEqual(attached["job_id"], created["job_id"])
        self.assertEqual(attached["status"], "RUNNING")
        self.assertFalse(attached["terminal"])
        self.assertEqual(attached["backend"]["type"], "pi-step")
        self.assertEqual(attached["backend"]["session_id"], "session-1")
        jobs.checkpoint(created["job_id"], summary="done", status="SUCCEEDED")
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            jobs.attach_backend(
                created["job_id"],
                {"type": "pi-step", "job_id": "step-2"},
            )

    def test_job_files_are_owner_only(self) -> None:
        created = jobs.create(goal="Private task")
        path = self.root / f"{created['job_id']}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
