import json
import sys
import tempfile
import os
from unittest.mock import patch
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from livingruntime_adapter_http import HTTPAgentAdapter
from agent_adapters import AdapterRegistry
import agent_sessions

class StubAgent(BaseHTTPRequestHandler):
    state = {"status":"WAITING"}
    requests=[]
    def log_message(self,*args):
        pass
    def respond(self,data):
        raw=json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
        self.requests.append((self.command,self.path,body))
        if self.path=="/sessions":
            return self.respond({"session_id":"native-1"})
        if self.path.endswith("/cancel"):
            self.state["status"]="CANCELLED"
        return self.respond({"status":self.state["status"]})
    def do_GET(self):
        self.requests.append((self.command,self.path,{}))
        if "/events?" in self.path:
            return self.respond({"events":[{"type":"progress","seq":1}]})
        return self.respond({"status":self.state["status"]})

class HTTPAdapterTests(unittest.TestCase):
    def test_loopback_round_trip(self):
        StubAgent.state={"status":"WAITING"}
        StubAgent.requests=[]
        server=ThreadingHTTPServer(("127.0.0.1",0),StubAgent)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            adapter=HTTPAgentAdapter("http://127.0.0.1:"+str(server.server_port),"test-token")
            sid=adapter.start("goal",project="demo",device="main")
            self.assertEqual(sid,"native-1")
            self.assertEqual(adapter.status(sid)["status"],"WAITING")
            adapter.send(sid,"hello")
            self.assertEqual(adapter.resume(sid)["status"],"WAITING")
            self.assertEqual(adapter.events(sid,after=0)[0]["seq"],1)
            adapter.cancel(sid)
            self.assertEqual(adapter.status(sid)["status"],"CANCELLED")
            self.assertTrue(any(row[1].endswith("/messages") for row in StubAgent.requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
    def test_two_live_http_adapters_and_durable_jobs(self):
        class FirstHandler(StubAgent):
            state={"status":"WAITING"}
            requests=[]
        class SecondHandler(StubAgent):
            state={"status":"WAITING"}
            requests=[]
        class FirstAdapter(HTTPAgentAdapter):
            adapter_id="http-first"
        class SecondAdapter(HTTPAgentAdapter):
            adapter_id="http-second"
        servers=[ThreadingHTTPServer(("127.0.0.1",0),FirstHandler),
                 ThreadingHTTPServer(("127.0.0.1",0),SecondHandler)]
        threads=[threading.Thread(target=x.serve_forever,daemon=True) for x in servers]
        for thread in threads:
            thread.start()
        try:
            with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{
                "LIVINGRUNTIME_AGENT_SESSIONS":str(Path(root)/"sessions"),
                "LIVINGRUNTIME_REMOTE_JOBS":str(Path(root)/"jobs")}):
                installed=AdapterRegistry()
                installed.register(FirstAdapter("http://127.0.0.1:"+str(servers[0].server_port)))
                installed.register(SecondAdapter("http://127.0.0.1:"+str(servers[1].server_port)))
                with patch.object(agent_sessions,"registry",installed):
                    a=agent_sessions.start("http-first","alpha",project="test",device="main",authorized=True)
                    b=agent_sessions.start("http-second","beta",project="test",device="main",authorized=True)
                    self.assertNotEqual(a["runtime_job_id"],b["runtime_job_id"])
                    agent_sessions.send(a["session_id"],"alpha message")
                    agent_sessions.cancel(a["session_id"])
                    self.assertEqual(agent_sessions.jobs.get(a["runtime_job_id"])["status"],"CANCELLED")
                    self.assertEqual(agent_sessions.status(b["session_id"])["agent_status"]["status"],"WAITING")
                    self.assertEqual(agent_sessions.resume(b["session_id"])["agent_status"]["status"],"WAITING")
                    self.assertEqual(len(agent_sessions.events(b["session_id"])["events"]),1)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)

    def test_reject_remote_endpoint(self):
        for address in ["https://example.com","http://example.com","file:///tmp/foo","http://user:password@localhost:1234"]:
            with self.assertRaises(ValueError):
                HTTPAgentAdapter(address)
if __name__=="__main__":
    unittest.main()
