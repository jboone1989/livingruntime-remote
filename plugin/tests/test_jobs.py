from __future__ import annotations

import json
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

        jobs.update_runtime(
            created["job_id"],
            runtime={"worker_alive": True, "child_alive": False},
            status="RUNNING",
        )
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

    def test_terminal_transition_creates_durable_completion_event(self) -> None:
        created = jobs.create(goal="Durable completion")
        completed = jobs.checkpoint(
            created["job_id"],
            summary="done",
            status="SUCCEEDED",
        )
        event = completed["completion_event"]
        self.assertTrue(event["event_id"].startswith("lrcomp_"))
        self.assertEqual(event["job_id"], created["job_id"])
        self.assertEqual(event["status"], "SUCCEEDED")
        self.assertTrue(event["terminal"])
        self.assertEqual(event["delivery_state"], "PENDING")
        self.assertEqual(event["delivery_attempts"], 0)
        self.assertIsNone(event["acknowledged_at"])
        acknowledged = jobs.acknowledge_completion(
            created["job_id"],
            event["event_id"],
        )
        self.assertIsNotNone(acknowledged["completion_event"]["acknowledged_at"])
        self.assertEqual(acknowledged["completion_event"]["delivery_state"], "ACKED")

    def test_completion_delivery_is_leased_redeliverable_and_acknowledged(self) -> None:
        created = jobs.create(goal="Reliable handoff")
        with patch.object(jobs.time, "time", return_value=100.0):
            completed = jobs.checkpoint(
                created["job_id"],
                summary="done",
                status="SUCCEEDED",
            )
            event_id = completed["completion_event"]["event_id"]
            first = jobs.claim_completion(
                created["job_id"],
                event_id,
                session_id="session-a",
                claim_seconds=300,
            )
        self.assertTrue(first["claimed"])
        self.assertEqual(first["completion_event"]["delivery_state"], "CLAIMED")
        self.assertEqual(first["completion_event"]["delivery_attempts"], 1)

        with patch.object(jobs.time, "time", return_value=101.0):
            same = jobs.claim_completion(
                created["job_id"],
                event_id,
                session_id="session-a",
            )
            other = jobs.claim_completion(
                created["job_id"],
                event_id,
                session_id="session-b",
            )
            delivered = jobs.mark_completion_delivered(
                created["job_id"],
                event_id,
                session_id="session-a",
            )
        self.assertEqual(same["reason"], "ALREADY_CLAIMED")
        self.assertEqual(same["completion_event"]["delivery_attempts"], 1)
        self.assertFalse(other["claimed"])
        self.assertEqual(other["reason"], "LEASED")
        self.assertEqual(delivered["completion_event"]["delivery_state"], "DELIVERED")

        with patch.object(jobs.time, "time", return_value=401.0):
            recovered = jobs.claim_completion(
                created["job_id"],
                event_id,
                session_id="session-b",
            )
        self.assertTrue(recovered["claimed"])
        self.assertEqual(recovered["completion_event"]["delivery_state"], "CLAIMED")
        self.assertEqual(recovered["completion_event"]["delivery_attempts"], 2)
        self.assertEqual(recovered["completion_event"]["claimed_by_session"], "session-b")

        acknowledged = jobs.acknowledge_completion(
            created["job_id"],
            event_id,
            acknowledged_by="chatgpt-session-b",
        )
        event = acknowledged["completion_event"]
        self.assertEqual(event["delivery_state"], "ACKED")
        self.assertIsNone(event["claimed_by_session"])
        self.assertIsNone(event["claim_expires_at"])

    def test_terminal_job_cannot_reopen(self) -> None:
        created = jobs.create(goal="One way")
        jobs.checkpoint(created["job_id"], summary="done", status="FAILED")
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            jobs.checkpoint(created["job_id"], summary="retry", status="RUNNING")

    def test_legacy_completion_event_is_backfilled_as_pending(self) -> None:
        created = jobs.create(goal="Legacy completion")
        jobs.checkpoint(created["job_id"], summary="done", status="SUCCEEDED")
        path = self.root / f"{created['job_id']}.json"
        raw = json.loads(path.read_text())
        event = dict(raw["completion_event"])
        for key in (
            "delivery_state",
            "delivery_attempts",
            "last_delivery_at",
            "claimed_by_session",
            "claim_expires_at",
            "delivered_at",
        ):
            event.pop(key, None)
        raw["completion_event"] = event
        path.write_text(json.dumps(raw))

        loaded = jobs.get(created["job_id"])
        self.assertEqual(loaded["completion_event"]["delivery_state"], "PENDING")
        self.assertEqual(loaded["completion_event"]["delivery_attempts"], 0)

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
        self.assertEqual(attached["status"], "PENDING")
        self.assertFalse(attached["terminal"])
        self.assertEqual(attached["backend"]["type"], "pi-step")
        self.assertEqual(attached["backend"]["session_id"], "session-1")
        jobs.checkpoint(created["job_id"], summary="done", status="SUCCEEDED")
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            jobs.attach_backend(
                created["job_id"],
                {"type": "pi-step", "job_id": "step-2"},
            )


    def test_runtime_heartbeat_updates_without_checkpoint_spam(self) -> None:
        created = jobs.create(
            goal="Long command",
            project="ferro",
            backend={
                "type": "exec",
                "job_id": "dex_123",
                "cwd": "/home/ubuntu/src/content-agent",
                "executable": "pytest",
            },
            status="PENDING",
        )
        updated = jobs.update_runtime(
            created["job_id"],
            runtime={
                "backend_status": "RUNNING",
                "observed_status": "RUNNING",
                "last_heartbeat_at": 123.0,
                "last_progress_at": 120.0,
                "heartbeat_age_seconds": 1.5,
                "progress_age_seconds": 4.5,
                "pid": 100,
                "child_pid": 101,
                "stdout_bytes": 55,
                "stderr_bytes": 2,
                "worker_alive": True,
                "child_alive": True,
            },
            status="RUNNING",
            current_step="pytest running",
        )
        self.assertEqual(updated["status"], "RUNNING")
        self.assertEqual(updated["runtime"]["child_pid"], 101)
        self.assertEqual(updated["runtime"]["last_heartbeat_at"], 123.0)
        self.assertTrue(updated["runtime"]["worker_alive"])
        self.assertTrue(updated["runtime"]["child_alive"])
        self.assertEqual(updated["backend"]["executable"], "pytest")
        self.assertEqual(updated["checkpoints"], [])

    def test_stalled_status_is_nonterminal_and_can_resume(self) -> None:
        created = jobs.create(goal="Potentially stalled")
        stalled = jobs.update_runtime(
            created["job_id"],
            runtime={
                "backend_status": "STALLED",
                "observed_status": "STALLED",
                "last_heartbeat_at": 10.0,
                "last_progress_at": 1.0,
            },
            status="STALLED",
        )
        self.assertFalse(stalled["terminal"])
        resumed = jobs.update_runtime(
            created["job_id"],
            runtime={
                "backend_status": "RUNNING",
                "observed_status": "RUNNING",
                "last_heartbeat_at": 20.0,
                "last_progress_at": 20.0,
                "worker_alive": True,
                "child_alive": False,
            },
            status="RUNNING",
        )
        self.assertEqual(resumed["status"], "RUNNING")
        self.assertFalse(resumed["terminal"])

    def test_running_cannot_be_fabricated_without_live_process(self) -> None:
        with self.assertRaisesRegex(ValueError, "live server process"):
            jobs.create(goal="Fake running", status="RUNNING")

        created = jobs.create(goal="Truthful state")
        with self.assertRaisesRegex(ValueError, "live server process"):
            jobs.checkpoint(
                created["job_id"],
                summary="pretend",
                status="RUNNING",
            )
        with self.assertRaisesRegex(ValueError, "live server process"):
            jobs.update_runtime(
                created["job_id"],
                runtime={"worker_alive": False, "child_alive": False},
                status="RUNNING",
            )
        with self.assertRaisesRegex(ValueError, "cannot be RUNNING"):
            jobs.attach_backend(
                created["job_id"],
                {"type": "pi", "job_id": "fake"},
                status="RUNNING",
            )

    def test_job_files_are_owner_only(self) -> None:
        created = jobs.create(goal="Private task")
        path = self.root / f"{created['job_id']}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
