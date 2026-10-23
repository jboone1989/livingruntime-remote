import json
import os
import sys
import time
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from livingruntime_adapter_claude import ClaudeCodeAdapter, validate_options
from claude_worker import main as run_worker, cli_args, public_events


class ClaudeAdapterTests(unittest.TestCase):
    def test_cli_options_are_native_and_no_artificial_turn_limit(self):
        record = {"executable": "/usr/bin/claude", "prompt": "investigate",
                  "cwd": "/tmp", "session_id": "00000000-0000-4000-8000-000000000001",
                  "options": {}}
        args = cli_args(record)
        self.assertIn("stream-json", args)
        self.assertIn("plan", args)
        self.assertNotIn("--max-turns", args)
        record["options"] = validate_options({
            "agent": "devils-advocate", "model": "sonnet", "effort": "high",
            "max_turns": 25, "permission_mode": "default", "duplex": True,
            "allowed_tools": ["Read", "Bash(git diff *)"],
            "agents": {"skeptic": {"description": "Review plans", "prompt": "Challenge assumptions"}},
        })
        args = cli_args(record)
        self.assertIn("--agent", args)
        self.assertIn("devils-advocate", args)
        self.assertIn("--permission-prompt-tool", args)
        self.assertIn("--input-format", args)
        self.assertIn("--agents", args)
        self.assertIn("--max-turns", args)
        self.assertIn("--allowedTools=Read,Bash(git diff *)", args)
        self.assertNotIn("investigate", args)  # supplied through duplex stdin
        record["continue"] = True
        self.assertIn("--resume", cli_args(record))

    def test_reject_unsafe_or_malformed_options(self):
        for bad in ({"permission_mode": "bypassPermissions"},
                    {"model": "--evil"}, {"unknown": True},
                    {"max_turns": -1}, {"duplex": "yes"},
                    {"allowed_tools": ["--flag"]}, {"agents": {}}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_options(bad)

    def test_public_events_keep_tool_names_not_raw_tool_arguments(self):
        raw = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tool_1", "name": "Bash",
             "input": {"command": "echo secret"}}, {"type": "text", "text": "review finished"}]}}
        events = public_events(raw)
        self.assertEqual(events[0]["kind"], "tool_call")
        self.assertNotIn("secret", json.dumps(events))
        self.assertEqual(events[1]["text"], "review finished")
        self.assertEqual(public_events({"type": "stream_event",
                       "event": {"delta": {"type": "text_delta", "text": "hello"}}})[0]["text"], "hello")

    def test_adapter_session_receipt_events_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "claude"
            cli.write_text("")
            env = {"LIVINGRUNTIME_CLAUDE_JOBS": str(Path(td) / "jobs"),
                   "LIVINGRUNTIME_CLAUDE_WORKSPACES": json.dumps({"main:demo": td})}
            with patch.dict(os.environ, env):
                a = ClaudeCodeAdapter(str(cli))
                with patch("livingruntime_adapter_claude.subprocess.Popen", return_value=Mock(pid=12345)):
                    sid = a.start("review", project="demo", device="main", options={"agent": "skeptic"})
                record = a._read(sid)
                self.assertEqual(record["options"]["agent"], "skeptic")
                event_path = a._path(sid).with_suffix(".events.jsonl")
                event_path.write_text(json.dumps({"seq": 1, "kind": "text_delta", "text": "review"}) + "\n")
                self.assertEqual(a.events(sid, after=0)[0]["text"], "review")
                receipt = a._path(sid).with_suffix(".result.json")
                receipt.write_text(json.dumps({"status": "SUCCEEDED", "result": "okay",
                                               "usage": {"num_turns": 3}, "finished_at": 123.0}))
                b = ClaudeCodeAdapter(str(cli))
                self.assertEqual(b.status(sid)["result"], "okay")
                self.assertEqual(b.status(sid)["usage"]["num_turns"], 3)
                with patch("livingruntime_adapter_claude.subprocess.Popen", return_value=Mock(pid=12346)):
                    b.send(sid, "follow up")
                self.assertTrue(b._read(sid)["continue"])

    def test_native_stream_worker_and_failure_receipts(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport json,sys\n"
                "print(json.dumps({'type':'system','subtype':'init','model':'sonnet'}))\n"
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','id':'one'}]}}))\n"
                "print(json.dumps({'type':'result','result':'native-ok','num_turns':15,'is_error':False}))\n"
            )
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "read-only review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "continue": False, "options": {"max_turns": 25},
            }))
            run_worker(str(record))
            receipt = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(receipt["status"], "SUCCEEDED", receipt)
            self.assertEqual(receipt["result"], "native-ok")
            self.assertEqual(receipt["usage"]["num_turns"], 15)
            events = [json.loads(x) for x in record.with_suffix(".events.jsonl").read_text().splitlines()]
            self.assertEqual(events[1]["kind"], "tool_call")
            self.assertEqual([e["seq"] for e in events], [1, 2, 3])
            cli.write_text("#!/usr/bin/python3\nprint('bad result')\n")
            run_worker(str(record))
            fail = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(fail["status"], "FAILED")
            self.assertIn("no result", fail["error"])

    def test_duplex_stream_permission_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport json,sys\n"
                "first=json.loads(sys.stdin.readline())\n"
                "assert first['type']=='user'\n"
                "print(json.dumps({'type':'control_request','request_id':'req_duplex',"
                "'request':{'tool_name':'Bash','input':{'command':'git status'}}}),flush=True)\n"
                "answer=json.loads(sys.stdin.readline())\n"
                "allowed=answer['response']['response']['behavior']=='allow'\n"
                "print(json.dumps({'type':'result','result':'approved' if allowed else 'denied',"
                "'is_error':False}),flush=True)\n"
            )
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "continue": False, "options": {"permission_mode": "default",
                                                 "duplex": True, "timeout_seconds": 30},
            }))
            worker = threading.Thread(target=run_worker, args=(str(record),), daemon=True)
            worker.start()
            events = record.with_suffix(".events.jsonl")
            for _ in range(100):
                if events.exists() and "req_duplex" in events.read_text():
                    break
                time.sleep(0.05)
            else:
                self.fail("no streamed native permission request")
            decision = record.with_suffix(".decisions.jsonl")
            decision.write_text(json.dumps({"request_id": "req_duplex", "decision": "allow",
                                            "input": {"command": "git status"}}) + "\n")
            worker.join(5)
            self.assertFalse(worker.is_alive(), "duplex worker stalled")
            receipt = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(receipt["status"], "SUCCEEDED", receipt)
            self.assertEqual(receipt["result"], "approved")

    def test_decisions_require_live_matching_permission(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "claude"
            cli.write_text("")
            with patch.dict(os.environ, {"LIVINGRUNTIME_CLAUDE_JOBS": td}):
                a = ClaudeCodeAdapter(str(cli))
                sid = "00000000-0000-4000-8000-000000000001"
                a._write(sid, {"session_id": sid, "status": "RUNNING", "started_at": time.time()})
                a._path(sid).with_suffix(".events.jsonl").write_text(json.dumps({
                    "seq": 1, "kind": "permission_request", "request_id": "req_1",
                    "input": {"command": "git status"}}) + "\n")
                with patch.object(a, "_worker_alive", return_value=True):
                    self.assertTrue(a.decide(sid, "req_1", "allow")["accepted"])
                    with self.assertRaises(ValueError):
                        a.decide(sid, "req_1", "allow")
                    with self.assertRaises(ValueError):
                        a.decide(sid, "unknown", "allow")


if __name__ == "__main__":
    unittest.main()
