import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from livingruntime_adapter_claude import ClaudeCodeAdapter
from claude_worker import main as run_worker

class ClaudeAdapterTests(unittest.TestCase):
    def test_detached_receipt_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            cli=Path(td)/"claude"
            cli.write_text("")
            with patch.dict(os.environ,{"LIVINGRUNTIME_CLAUDE_JOBS":str(Path(td)/"jobs")}):
                a=ClaudeCodeAdapter(str(cli))
                with self.assertRaises(PermissionError):
                    a.start("hello",project="demo",device="main")
                with patch.dict(os.environ,{"LIVINGRUNTIME_CLAUDE_WORKSPACES":json.dumps({"main:demo":td})}):
                    with patch("livingruntime_adapter_claude.subprocess.Popen",return_value=Mock(pid=12345)) as run:
                        sid=a.start("What is 2+2?",project="demo",device="main")
                        self.assertEqual(a.status(sid)["status"],"RUNNING")
                        argv=run.call_args.args[0]
                        self.assertIn("claude_worker.py",argv[1])
                        record=a._read(sid)
                        self.assertNotIn("bypassPermissions",str(record))
                        receipt=a._path(sid).with_suffix(".result.json")
                        receipt.write_text(json.dumps({"status":"SUCCEEDED","result":"4","finished_at":123.0}))
                        b=ClaudeCodeAdapter(str(cli))
                        self.assertEqual(b.status(sid)["result"],"4")
                        self.assertEqual(b.status(sid)["status"],"SUCCEEDED")
                        b.send(sid,"continue")
                        self.assertTrue(b._read(sid)["continue"])
                        self.assertEqual(run.call_count,2)
                        self.assertTrue(b.capabilities().cancel)

    def test_real_detached_worker_survives_adapter_restart(self):
        with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[2])) as td:
            cli=Path(td)/"mock-claude"
            cli.write_text("#!/usr/bin/python3\nimport json,sys\nassert sys.argv[sys.argv.index('--max-turns')+1]=='10'\nprint(json.dumps({'result':'worker-ok'}))\n")
            cli.chmod(0o700)
            with patch.dict(os.environ,{"LIVINGRUNTIME_CLAUDE_JOBS":str(Path(td)/"jobs"),
                                     "LIVINGRUNTIME_CLAUDE_WORKSPACES":json.dumps({"main:demo":td})}):
                adapter=ClaudeCodeAdapter(str(cli))
                sid=adapter.start("Verify worker",project="demo",device="main")
                restored=ClaudeCodeAdapter(str(cli))
                observed=None
                for _ in range(80):
                    observed=restored.status(sid)
                    if observed["status"] != "RUNNING":
                        break
                    time.sleep(0.1)
                self.assertEqual(observed["status"],"SUCCEEDED",observed)
                self.assertEqual(observed["result"],"worker-ok")

    def test_worker_invalid_output_creates_failure_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"session.json"
            path.write_text(json.dumps({
                "executable":"/bin/true","prompt":"read-only review","cwd":td,
                "session_id":"9ef98370-1111-4222-8333-777777777777", "continue":False
            }))
            for code,body,stderr,fragment in [(0,"[]","","no valid JSON result object"),
                                              (0,"not-json","","no valid JSON result object"),
                                              (1,"not-json","CLI failed","Claude exit=1: CLI failed"),
                                              (0,json.dumps({"is_error":True,"result":"tool limit"}),"","tool limit"),
                                              (0,json.dumps({"subtype":"error_max_turns"}),"","error_max_turns")]:
                with self.subTest(code=code,body=body):
                    with patch("claude_worker.subprocess.run",return_value=Mock(
                        returncode=code,stdout=body,stderr=stderr)):
                        run_worker(str(path))
                    receipt=json.loads(path.with_suffix(".result.json").read_text())
                    self.assertEqual(receipt["status"],"FAILED")
                    self.assertIn(fragment,receipt["error"])

if __name__=="__main__":
    unittest.main()
