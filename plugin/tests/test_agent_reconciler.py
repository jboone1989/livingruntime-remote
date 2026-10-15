import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import agent_reconciler
import agent_sessions
from agent_adapters import AdapterRegistry,AgentCapabilities

class Done:
    adapter_id="done"
    def capabilities(self):
        return AgentCapabilities()
    def start(self,goal,*,project,device):
        return "native"
    def status(self,session_id):
        return {"status":"SUCCEEDED"}

class ReconcileTests(unittest.TestCase):
    def test_terminal_state_emits_completion_without_manual_query(self):
        with tempfile.TemporaryDirectory() as td:
            env={"LIVINGRUNTIME_AGENT_SESSIONS":str(Path(td)/"sessions"),
                 "LIVINGRUNTIME_REMOTE_JOBS":str(Path(td)/"jobs")}
            reg=AdapterRegistry()
            reg.register(Done())
            with patch.dict(os.environ,env), patch.object(agent_sessions,"registry",reg):
                created=agent_sessions.start("done","goal",project="demo",device="main",authorized=True)
                self.assertEqual(agent_sessions.jobs.get(created["runtime_job_id"])["status"],"PENDING")
                summary=agent_reconciler.reconcile()
                self.assertEqual(summary["updated"],1)
                job=agent_sessions.jobs.get(created["runtime_job_id"])
                self.assertEqual(job["status"],"SUCCEEDED")
                self.assertIsNotNone(job["completion_event"])
                self.assertEqual(agent_reconciler.reconcile()["updated"],0)

if __name__=="__main__":
    unittest.main()
