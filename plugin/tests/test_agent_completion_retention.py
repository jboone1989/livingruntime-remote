import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import jobs

class AgentCompletionRetentionTests(unittest.TestCase):
    def test_delegated_receipt_needs_explicit_ack_even_after_retention(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{"LIVINGRUNTIME_REMOTE_JOBS":tmp}):
            agent=jobs.create(goal="agent",project="demo",device="main",
                              backend={"type":"agent-adapter","job_id":"session-a"})
            agent=jobs.checkpoint(agent["job_id"],summary="done",status="SUCCEEDED")
            event=agent["completion_event"]
            expired=jobs.expire_stale_completion_events(
                now=float(event["created_at"])+864000,retention_seconds=60)
            self.assertEqual(expired,0)
            retrieved=jobs.get(agent["job_id"])
            self.assertIsNone(retrieved["completion_event"]["acknowledged_at"])
            self.assertEqual(retrieved["completion_event"]["delivery_state"],"PENDING")

if __name__=="__main__":
    unittest.main()
