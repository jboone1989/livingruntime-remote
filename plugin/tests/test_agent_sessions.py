import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import agent_sessions
from agent_adapters import AgentCapabilities, AdapterRegistry

class Dummy:
    def __init__(self,name,can_cancel=True):
        self.adapter_id=name
        self.messages=[]
        self.cancelled=[]
        self.can_cancel=can_cancel
        self.next_status="WAITING"
    def capabilities(self):
        return AgentCapabilities(messaging=True,cancel=self.can_cancel,resume=True,events=True)
    def start(self,goal,*,project,device):
        return self.adapter_id+"-native-session"
    def status(self,session_id):
        return {"status":self.next_status,"native_session":session_id}
    def send(self,session_id,message):
        self.messages.append((session_id,message))
    def cancel(self,session_id):
        self.cancelled.append(session_id)
    def resume(self,session_id):
        return {"status":self.next_status,"native_session":session_id}
    def events(self,session_id,*,after=0):
        return [{"id":after+1,"type":"progress"}]

class AgentSessionsTests(unittest.TestCase):
    def test_zero_agents_and_multi_adapter_routing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{"LIVINGRUNTIME_AGENT_SESSIONS":tmp,"LIVINGRUNTIME_REMOTE_JOBS":str(Path(tmp)/"jobs")}):
            registry=AdapterRegistry()
            first=Dummy("first")
            second=Dummy("second",False)
            registry.register(first)
            registry.register(second)
            with patch.object(agent_sessions,"registry",registry):
                with self.assertRaises(KeyError):
                    agent_sessions.start("missing","goal",project="demo",device="main",authorized=True)
                with self.assertRaises(PermissionError):
                    agent_sessions.start("first","goal",project="demo",device="main")
                a=agent_sessions.start("first","goal",project="demo",device="main",authorized=True)
                b=agent_sessions.start("second","goal",project="demo",device="main",authorized=True)
                self.assertNotEqual(a["session_id"],b["session_id"])
                self.assertIn("runtime_job_id",a)
                self.assertEqual(agent_sessions.jobs.get(a["runtime_job_id"])["status"],"PENDING")
                self.assertEqual(agent_sessions.status(a["session_id"])["agent_status"]["native_session"],"first-native-session")
                self.assertEqual(agent_sessions.resume(a["session_id"])["agent_status"]["status"],"WAITING")
                self.assertEqual(agent_sessions.events(a["session_id"],after=1)["next_offset"],2)
                agent_sessions.send(a["session_id"],"hello")
                self.assertEqual(first.messages,[("first-native-session","hello")])
                self.assertEqual(second.messages,[])
                second.next_status="SUCCEEDED"
                self.assertEqual(agent_sessions.status(b["session_id"])["agent_status"]["status"],"SUCCEEDED")
                self.assertEqual(agent_sessions.jobs.get(b["runtime_job_id"])["status"],"SUCCEEDED")
                agent_sessions.status(b["session_id"])
                with self.assertRaises(NotImplementedError):
                    agent_sessions.cancel(b["session_id"])
                agent_sessions.cancel(a["session_id"])
                self.assertEqual(first.cancelled,["first-native-session"])
                self.assertEqual(agent_sessions.jobs.get(a["runtime_job_id"])["status"],"CANCELLED")
                self.assertEqual(agent_sessions.get(a["session_id"])["state"],"CANCELLED")
                self.assertTrue(agent_sessions.cancel(a["session_id"])["already_cancelled"])
                with self.assertRaisesRegex(RuntimeError,"cancelled"):
                    agent_sessions.send(a["session_id"],"should not send")
                with self.assertRaises(PermissionError):
                    agent_sessions._resolve(a["session_id"],"second")
                self.assertEqual((Path(tmp)/(a["session_id"]+".json")).stat().st_mode & 0o777,0o600)

if __name__=="__main__":
    unittest.main()
