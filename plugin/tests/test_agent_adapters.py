import sys
import os
import tempfile
import json
from unittest.mock import patch
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from agent_adapters import AgentCapabilities, AdapterRegistry, registry, load_installed

class FakeAgent:
    adapter_id = "mock-agent"
    def capabilities(self):
        return AgentCapabilities(resume=True, messaging=True)
    def start(self, goal, *, project, device):
        return "mock-session"
    def status(self, session_id):
        return {"status": "WAITING", "session_id": session_id}
    def send(self, session_id, message):
        pass
    def resume(self, session_id):
        return self.status(session_id)
    def cancel(self, session_id):
        pass

class AgentAdapterTests(unittest.TestCase):
    def test_zero_agent_mode(self):
        empty=AdapterRegistry()
        self.assertEqual(empty.list_agents(), [])
        with self.assertRaises(KeyError):
            empty.get("mock-agent")

    def test_register_and_capabilities(self):
        items=AdapterRegistry()
        items.register(FakeAgent())
        self.assertEqual(items.get("mock-agent").start("goal", project="repo", device="main"), "mock-session")
        self.assertTrue(items.list_agents()[0]["capabilities"]["resume"])
        self.assertFalse(items.list_agents()[0]["capabilities"]["events"])
        with self.assertRaises(ValueError):
            items.register(FakeAgent())

    def test_instances_are_isolated(self):
        one=AdapterRegistry()
        two=AdapterRegistry()
        one.register(FakeAgent())
        self.assertEqual(two.list_agents(), [])

    def test_manifest_opt_in_and_invalid_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"adapters.json"
            with patch.dict(os.environ,{"LIVINGRUNTIME_AGENT_ADAPTERS":str(target)}):
                self.assertEqual(load_installed(AdapterRegistry()),[])
                target.write_text(json.dumps({"adapters":[{"module":"untrusted","factory":"make"}]}))
                with self.assertRaisesRegex(ValueError,"namespace"):
                    load_installed(AdapterRegistry())
                target.write_text(json.dumps({"adapters":[]}))
                self.assertEqual(load_installed(AdapterRegistry()),[])

if __name__=="__main__":
    unittest.main()
