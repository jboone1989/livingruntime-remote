"""Native Codex CLI Adapter tests: fake CLI implements official JSONL protocol."""
from __future__ import annotations
import json
import os
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from livingruntime_adapter_codex import CodexAdapter, validate_options
from codex_worker import cli_args, public_events


THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"


class CodexAdapterTests(unittest.TestCase):
    def test_option_validation_and_native_command(self):
        self.assertEqual(validate_options({"sandbox": "workspace-write", "timeout_seconds": 60}),
                         {"sandbox": "workspace-write", "timeout_seconds": 60})
        for value in ({"sandbox": "danger-full-access"}, {"model": "--hack"},
                      {"timeout_seconds": 1}, {"skip_git_repo_check": "yes"}, {"extra": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_options(value)
        base = {"executable": "/usr/bin/codex", "prompt": "--yolo",
                "cwd": "/work", "options": {}, "session_id": str(uuid.uuid4())}
        cmd = cli_args(base)
        self.assertEqual(cmd[:3], ["/usr/bin/codex", "exec", "--json"])
        self.assertIn("read-only", cmd)
        self.assertEqual(cmd[-2:], ["--", "--yolo"])
        with tempfile.TemporaryDirectory() as td:
            record = Path(td) / "session.json"
            record.with_suffix(".thread").write_text(THREAD)
            resume = cli_args({**base, "continue": True, "path": str(record)})
            self.assertIn("resume", resume)
            self.assertIn("--sandbox", resume)
            self.assertIn("read-only", resume)
            self.assertEqual(resume[-2:], ["--", "--yolo"])
            self.assertNotIn("--skip-git-repo-check", resume)
            base["options"] = {"skip_git_repo_check": True}
            checked = cli_args({**base, "continue": True, "path": str(record)})
            self.assertIn("--skip-git-repo-check", checked)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_json_events_remove_tool_arguments_and_output(self):
        event = {"type": "item.started", "item": {"id": "x1", "type": "command_execution",
                                                  "command": "echo PRIVATE"}}
        out = public_events(event)
        self.assertEqual(out[0]["name"], "command_execution")
        self.assertNotIn("PRIVATE", json.dumps(out))
        out = public_events({"type": "item.completed", "item": {
            "type": "command_execution", "id": "x1", "status": "completed",
            "aggregated_output": "SECRET"}})
        self.assertTrue(out[0]["success"])
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertEqual(public_events({"type": "turn.completed",
                                        "usage": {"input_tokens": 2, "output_tokens": 1}})[0]["kind"],
                         "turn_result")
        self.assertEqual(public_events({"type": "turn.failed", "error": {"message": "Failure"}})[0]["kind"],
                         "error")

    def test_fake_native_cli_start_resume_and_event_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "codex"
            fake.write_text(
                "#!/usr/bin/python3\n"
                "import sys,json\n"
                "args=sys.argv[1:]\n"
                "assert args[0]=='exec',args\n"
                "resume='resume' in args\n"
                "assert '--json' in args,args\n"
                "if resume: assert '" + THREAD + "' in args,args\n"
                "else: assert '--sandbox' in args and 'read-only' in args,args\n"
                "print(json.dumps({'type':'thread.started','thread_id':'" + THREAD + "'}),flush=True)\n"
                "print(json.dumps({'type':'turn.started'}),flush=True)\n"
                "print(json.dumps({'type':'item.started','item':{'id':'tool_1','type':'command_execution','command':'echo SECRET'}}),flush=True)\n"
                "print(json.dumps({'type':'item.completed','item':{'id':'tool_1','type':'command_execution','status':'completed','aggregated_output':'SECRET'}}),flush=True)\n"
                "print(json.dumps({'type':'item.completed','item':{'id':'msg_1','type':'agent_message','text':"
                "('resumed context' if resume else 'first turn')}}),flush=True)\n"
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':5}}),flush=True)\n"
            )
            fake.chmod(0o700)
            with (patch.dict(os.environ, {"LIVINGRUNTIME_CODEX_JOBS": td}),
                  patch("livingruntime_adapter_codex.claude_workspace", return_value=td)):
                adapter = CodexAdapter(str(fake))
                sid = adapter.start("review this code", project="test", device="main")
                for _ in range(120):
                    first = adapter.status(sid)
                    if first["status"] != "RUNNING":
                        break
                    time.sleep(.05)
                self.assertEqual(first["status"], "SUCCEEDED", first)
                self.assertEqual(first["result"], "first turn")
                first_events = adapter.events(sid)
                self.assertEqual(first_events[0]["kind"], "session")
                self.assertEqual(first_events[-1]["kind"], "completion")
                self.assertNotIn("SECRET", json.dumps(first_events))
                self.assertEqual((Path(td) / (sid + ".thread")).read_text(), THREAD)
                adapter.send(sid, "continue previous review")
                for _ in range(120):
                    second = adapter.status(sid)
                    if second["status"] == "SUCCEEDED":
                        break
                    time.sleep(.05)
                self.assertEqual(second["status"], "SUCCEEDED", second)
                self.assertEqual(second["result"], "resumed context")
                events = adapter.events(sid)
                self.assertTrue(len(events) > len(first_events))
                self.assertEqual([x["seq"] for x in events], list(range(1, len(events) + 1)))
                self.assertEqual(len([x for x in events if x["kind"] == "completion"]), 2)

    def test_remote_cancel_reaps_native_codex_worker(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "codex"
            fake.write_text(
                "#!/usr/bin/python3\nimport json,time\n"
                "print(json.dumps({'type':'thread.started','thread_id':'" + THREAD + "'}),flush=True)\n"
                "time.sleep(60)\n"
            )
            fake.chmod(0o700)
            with (patch.dict(os.environ, {"LIVINGRUNTIME_CODEX_JOBS": td}),
                  patch("livingruntime_adapter_codex.claude_workspace", return_value=td)):
                adapter = CodexAdapter(str(fake))
                sid = adapter.start("test cancel", project="test", device="main")
                events_path = Path(td) / (sid + ".events.jsonl")
                try:
                    for _ in range(100):
                        if events_path.exists() and '"kind": "session"' in events_path.read_text():
                            break
                        time.sleep(.05)
                    else:
                        self.fail("Codex worker did not reach native thread init")
                    adapter.cancel(sid)
                    receipt = Path(td) / (sid + ".result.json")
                    for _ in range(100):
                        if receipt.exists():
                            break
                        time.sleep(.05)
                    self.assertTrue(receipt.exists(), "cancel must leave a native worker receipt")
                    value = json.loads(receipt.read_text())
                    self.assertEqual(value["status"], "CANCELLED", value)
                    self.assertEqual(adapter.events(sid)[-1]["kind"], "completion")
                finally:
                    try:
                        adapter.cancel(sid)
                    except (OSError, RuntimeError):
                        pass

    def test_transient_error_before_turn_completed_is_not_failure(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "codex"
            fake.write_text(
                "#!/usr/bin/python3\nimport json\n"
                "print(json.dumps({'type':'thread.started','thread_id':'" + THREAD + "'}),flush=True)\n"
                "print(json.dumps({'type':'error','message':'Reconnecting... 1/5'}),flush=True)\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message',"
                "'id':'msg_1','text':'RECOVERED'}}),flush=True)\n"
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':4}}),flush=True)\n"
            )
            fake.chmod(0o700)
            with (patch.dict(os.environ, {"LIVINGRUNTIME_CODEX_JOBS": td}),
                  patch("livingruntime_adapter_codex.claude_workspace", return_value=td)):
                adapter = CodexAdapter(str(fake))
                sid = adapter.start("handle transient failure", project="test", device="main")
                for _ in range(120):
                    result = adapter.status(sid)
                    if result["status"] != "RUNNING":
                        break
                    time.sleep(.05)
                self.assertEqual(result["status"], "SUCCEEDED", result)
                self.assertEqual(result["result"], "RECOVERED")

    def test_nonzero_cli_exit_keeps_native_quota_failure_reason(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "codex"
            fake.write_text(
                "#!/usr/bin/python3\nimport json,sys\n"
                "print(json.dumps({'type':'thread.started','thread_id':'" + THREAD + "'}),flush=True)\n"
                "print(json.dumps({'type':'turn.failed','error':{'message':'quota exhausted'}}),flush=True)\n"
                "sys.exit(1)\n"
            )
            fake.chmod(0o700)
            with (patch.dict(os.environ, {"LIVINGRUNTIME_CODEX_JOBS": td}),
                  patch("livingruntime_adapter_codex.claude_workspace", return_value=td)):
                adapter = CodexAdapter(str(fake))
                sid = adapter.start("review", project="test", device="main")
                for _ in range(120):
                    outcome = adapter.status(sid)
                    if outcome["status"] in ("FAILED", "SUCCEEDED", "STALLED", "CANCELLED"):
                        break
                    time.sleep(.05)
                self.assertEqual(outcome["status"], "FAILED", outcome)
                self.assertIn("quota exhausted", outcome["error"])
                self.assertTrue(any(e["kind"] == "error" and "quota exhausted" in e["message"]
                                    for e in adapter.events(sid)))

    def test_missing_binary_fails_only_at_launch(self):
        missing = CodexAdapter("/no/such/codex")
        self.assertEqual(missing.adapter_id, "codex")
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            missing.start("check", project="test", device="main")


if __name__ == "__main__":
    unittest.main()
