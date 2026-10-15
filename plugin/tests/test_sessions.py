import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sessions
import jobs

class SessionTests(unittest.TestCase):
    def test_reconnect_replay_ack(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_SESSIONS": root}):
            s = sessions.create("Finish isolated test", device="main", project="livingruntime-remote")
            sid = s["session_id"]
            sessions.receive(sid, event_id="ev1", job_id="job1", status="SUCCEEDED")
            sessions.receive(sid, event_id="ev1", job_id="job1", status="SUCCEEDED")
            self.assertEqual(len(sessions.pending(sid)), 1)
            self.assertEqual(sessions.get(sid)["goal"], "Finish isolated test")
            with self.assertRaisesRegex(ValueError, "conflicting"):
                sessions.receive(sid, event_id="ev1", job_id="job2", status="FAILED")
            sessions.acknowledge(sid, "ev1")
            self.assertEqual(sessions.pending(sid), [])
            sessions.receive(sid, event_id="ev1", job_id="job1", status="SUCCEEDED")
            self.assertEqual(sessions.pending(sid), [])
            self.assertEqual((_path := Path(root) / (sid + ".json")).stat().st_mode & 0o777, 0o600)

    def test_invalid_and_unknown_event(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LIVINGRUNTIME_REMOTE_SESSIONS": root}):
            sid = sessions.create("goal", device="main", project="repo")["session_id"]
            with self.assertRaises(ValueError):
                sessions.receive(sid, event_id="../bad", job_id="job1", status="SUCCEEDED")
            with self.assertRaises(KeyError):
                sessions.acknowledge(sid, "missing")


    def test_durable_job_completion_can_be_replayed_to_session(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "LIVINGRUNTIME_REMOTE_SESSIONS": str(Path(root) / "sessions"),
            "LIVINGRUNTIME_REMOTE_JOBS": str(Path(root) / "jobs"),
        }):
            sid = sessions.create("Verify outcome", device="main", project="livingruntime-remote")["session_id"]
            job = jobs.create(goal="Run test", project="livingruntime-remote", device="main")
            completed = jobs.checkpoint(job["job_id"], summary="test passed", status="SUCCEEDED")
            ev = completed["completion_event"]
            for _ in range(2):
                sessions.receive(sid, event_id=ev["event_id"], job_id=job["job_id"], status=ev["status"])
            self.assertEqual(len(sessions.pending(sid)), 1)
            self.assertEqual(sessions.pending(sid)[0]["job_id"], job["job_id"])
            sessions.acknowledge(sid, ev["event_id"])
            self.assertEqual(sessions.pending(sid), [])
            self.assertIsNone(jobs.get(job["job_id"])["completion_event"]["acknowledged_at"])
            jobs.acknowledge_completion(job["job_id"], ev["event_id"], acknowledged_by="test-controller")
            self.assertEqual(jobs.get(job["job_id"])["completion_event"]["delivery_state"], "ACKED")

if __name__ == "__main__":
    unittest.main()
