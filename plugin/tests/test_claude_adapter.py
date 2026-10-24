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
        record["prompt"] = "--dangerously-skip-permissions"
        safe = cli_args(record)
        self.assertEqual(safe[-2:], ["--", "--dangerously-skip-permissions"])
        self.assertEqual(safe.count("--dangerously-skip-permissions"), 1)
        record["prompt"] = "investigate"
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

    def test_native_dev_events_preserve_subagent_mcp_and_hook_context(self):
        init = public_events({"type": "system", "subtype": "init", "model": "sonnet",
                              "mcp_servers": [{"name": "docs", "status": "connected"},
                                              {"name": "bad", "status": "failed"}],
                              "skills": ["thinking-in-systems"], "agents": ["repo-inspector"]})
        self.assertEqual(init[0]["mcp_servers"][1]["status"], "failed")
        self.assertEqual(init[0]["skills"], ["thinking-in-systems"])
        self.assertEqual(public_events({"type": "system", "subtype": "hook_started",
                                         "hook_name": "PreToolUse"})[0]["kind"], "hook_started")
        self.assertEqual(public_events({"type": "system", "subtype": "task_started",
                                         "task_id": "task-one"})[0]["task_id"], "task-one")
        raw = {"type": "assistant", "parent_tool_use_id": "parent_1", "message": {"content": [
            {"type": "tool_use", "name": "Agent", "id": "one",
             "input": {"subagent_type": "repo-inspector", "prompt": "private secret"}},
            {"type": "tool_use", "name": "Skill", "id": "two",
             "input": {"skill": "thinking-in-systems", "args": "private secret"}}]}}
        events = public_events(raw)
        self.assertEqual(events[0]["subagent_type"], "repo-inspector")
        self.assertEqual(events[0]["parent_tool_use_id"], "parent_1")
        self.assertEqual(events[1]["skill"], "thinking-in-systems")
        self.assertNotIn("private secret", json.dumps(events))
        tool_result = public_events({"type": "user", "parent_tool_use_id": "parent_1",
                                    "message": {"content": [{"type": "tool_result",
                                    "tool_use_id": "one", "content": "secret output"}]}})
        self.assertEqual(tool_result[0]["parent_tool_use_id"], "parent_1")
        self.assertNotIn("secret output", json.dumps(tool_result))

    def test_active_streaming_does_not_hit_idle_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport json,time\n"
                "for i in range(5):\n"
                " print(json.dumps({'type':'stream_event','event':"
                "{'delta':{'type':'text_delta','text':'tick'}}}),flush=True)\n"
                " time.sleep(0.35)\n"
                "print(json.dumps({'type':'result','result':'completed','is_error':False}),flush=True)\n"
            )
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "options": {"timeout_seconds": 1},
            }))
            run_worker(str(record))
            result = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertEqual(result["result"], "completed")

    def test_idle_timeout_reaps_cli_children_holding_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport sys,time,signal,subprocess,json\n"
                "subprocess.Popen([sys.executable,'-c',"
                "'import time,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],"
                "stdout=sys.stdout,stderr=sys.stderr)\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "print(json.dumps({'type':'system','subtype':'init','model':'fake'}),flush=True)\n"
                "time.sleep(60)\n"
            )
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "options": {"timeout_seconds": 1},
            }))
            worker = threading.Thread(target=run_worker, args=(str(record),), daemon=True)
            worker.start()
            worker.join(8)
            self.assertFalse(worker.is_alive(), "orphan CLI child kept stdout open after timeout")
            receipt = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(receipt["status"], "STALLED", receipt)

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
                self.assertIn("review", event_path.read_text())  # follow-up retains event history

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
            self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4])
            cli.write_text("#!/usr/bin/python3\nprint('bad result')\n")
            run_worker(str(record))
            fail = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(fail["status"], "FAILED")
            self.assertIn("no result", fail["error"])

    def test_background_turn_result_is_not_session_completion(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport json,time,sys\n"
                "sys.stdin.readline()\n"
                "print(json.dumps({'type':'result','result':'foreground done','is_error':False}),flush=True)\n"
                "time.sleep(0.4)\n"
                "print(json.dumps({'type':'system','subtype':'task_notification','task_id':'bg1'}),flush=True)\n"
                "print(json.dumps({'type':'result','result':'background done','is_error':False}),flush=True)\n"
                "sys.stdin.read()\n"
            )
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "options": {"duplex": True, "timeout_seconds": 2},
            }))
            run_worker(str(record))
            result = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertEqual(result["result"], "background done")
            events = [json.loads(x) for x in record.with_suffix(".events.jsonl").read_text().splitlines()]
            self.assertEqual([e["kind"] for e in events],
                             ["turn_result", "task_notification", "turn_result", "completion"])
            self.assertEqual(events[-1]["status"], "SUCCEEDED")

    def test_duplex_background_result_keeps_idle_monitor_running(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport json,time,sys\n"
                "sys.stdin.readline()\n"
                "print(json.dumps({'type':'result','result':'foreground done','is_error':False}),flush=True)\n"
                "time.sleep(8)\n"
            )
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "options": {"duplex": True, "timeout_seconds": 1},
            }))
            worker = threading.Thread(target=run_worker, args=(str(record),), daemon=True)
            worker.start()
            worker.join(6)
            self.assertFalse(worker.is_alive(), "worker lost idle timeout after early result")
            receipt = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(receipt["status"], "STALLED", receipt)
            events = [json.loads(x) for x in record.with_suffix(".events.jsonl").read_text().splitlines()]
            self.assertEqual(events[0]["kind"], "turn_result")
            self.assertEqual(events[-1]["kind"], "completion")
            self.assertEqual(events[-1]["status"], "STALLED")

    def test_native_structured_output_without_text(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "claude"
            cli.write_text("#!/usr/bin/python3\nimport json\n"
                           "print(json.dumps({'type':'result','result':'',"
                           "'structured_output':{'answer':42},'is_error':False}))\n")
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "structured response", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001", "options": {},
            }))
            run_worker(str(record))
            receipt = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(receipt["status"], "SUCCEEDED", receipt)
            self.assertEqual(receipt["structured_output"], {"answer": 42})

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
                "sys.stdin.read()\n"  # real duplex CLI waits for EOF even after final result
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

    def test_scoped_native_cli_feature_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "skills").mkdir()
            (root / "mcp.json").write_text('{"mcpServers": {}}')
            cli = root / "claude"
            cli.write_text("")
            env = {"LIVINGRUNTIME_CLAUDE_JOBS": str(root / "jobs"),
                   "LIVINGRUNTIME_CLAUDE_WORKSPACES": json.dumps({"main:demo": td})}
            with patch.dict(os.environ, env):
                adapter = ClaudeCodeAdapter(str(cli))
                options = {"plugin_dirs": ["skills"], "mcp_configs": ["mcp.json"],
                           "fallback_model": "sonnet,haiku", "forward_subagent_text": True,
                           "include_hook_events": True, "max_budget_usd": 2.0,
                           "json_schema": {"type": "object"}}
                with patch("livingruntime_adapter_claude.subprocess.Popen", return_value=Mock(pid=12345)):
                    sid = adapter.start("test", project="demo", device="main", options=options)
                args = cli_args(adapter._read(sid))
                for flag in ("--plugin-dir", "--mcp-config", "--forward-subagent-text",
                             "--include-hook-events", "--json-schema", "--max-budget-usd"):
                    self.assertIn(flag, args)
                with self.assertRaises(PermissionError):
                    adapter.start("test", project="demo", device="main",
                                  options={"plugin_dirs": [".."]})
                with self.assertRaises(ValueError):
                    adapter.start("test", project="demo", device="main",
                                  options={"mcp_configs": ["skills"]})

    def test_events_have_monotonic_sequences_across_turns(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text("#!/usr/bin/python3\nimport json\n"
                           "print(json.dumps({'type':'result','result':'done','is_error':False}))\n")
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "continue": False, "options": {},
            }))
            run_worker(str(record))
            record.write_text(json.dumps({**json.loads(record.read_text()), "continue": True}))
            run_worker(str(record))
            events = [json.loads(x) for x in record.with_suffix(".events.jsonl").read_text().splitlines()]
            self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4])

    def test_native_ask_user_question_answers(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport json,sys\n"
                "first=json.loads(sys.stdin.readline())\n"
                "assert first['type']=='user'\n"
                "request={'type':'control_request','request_id':'ask_1',"
                "'request':{'tool_name':'AskUserQuestion','input':{'questions':["
                "{'question':'Which approach?','multiSelect':False,'options':[{'label':'Reuse'},{'label':'Rewrite'}]}]}}}\n"
                "print(json.dumps(request),flush=True)\n"
                "response=json.loads(sys.stdin.readline())['response']['response']\n"
                "assert response['behavior']=='allow'\n"
                "assert response['updatedInput']['answers']=={'Which approach?':'Reuse'}\n"
                "print(json.dumps({'type':'result','result':'Reuse selected','is_error':False}),flush=True)\n"
                "sys.stdin.read()\n"
            )
            cli.chmod(0o700)
            sid = "00000000-0000-4000-8000-000000000001"
            with patch.dict(os.environ, {"LIVINGRUNTIME_CLAUDE_JOBS": td}):
                adapter = ClaudeCodeAdapter(str(cli))
                record = adapter._path(sid)
                adapter._write(sid, {
                    "executable": str(cli), "prompt": "Choose", "cwd": td,
                    "session_id": sid, "status": "RUNNING", "started_at": time.time(),
                    "options": {"duplex": True, "timeout_seconds": 2},
                })
                worker = threading.Thread(target=run_worker, args=(str(record),), daemon=True)
                worker.start()
                event_path = record.with_suffix(".events.jsonl")
                for _ in range(100):
                    if event_path.exists() and '"request_id": "ask_1"' in event_path.read_text():
                        break
                    time.sleep(0.04)
                else:
                    self.fail("CLI did not produce native question")
                with patch.object(adapter, "_worker_alive", return_value=True):
                    with self.assertRaisesRegex(ValueError, "requires structured answers"):
                        adapter.decide(sid, "ask_1", "allow")
                    with self.assertRaisesRegex(ValueError, "match Claude"):
                        adapter.decide(sid, "ask_1", "allow", answers={"Wrong question": "Reuse"})
                    time.sleep(2.2)  # beyond the 2-second active task budget
                    self.assertTrue(worker.is_alive(), "human approval incorrectly timed out")
                    adapter.decide(sid, "ask_1", "allow", answers={"Which approach?": "Reuse"})
                worker.join(8)
                self.assertFalse(worker.is_alive())
                receipt = json.loads(record.with_suffix(".result.json").read_text())
                self.assertEqual(receipt["status"], "SUCCEEDED", receipt)
                self.assertEqual(receipt["result"], "Reuse selected")

    def test_unanswered_native_permission_has_independent_wait_limit(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / "fake-claude"
            cli.write_text(
                "#!/usr/bin/python3\nimport json,sys\n"
                "sys.stdin.readline()\n"  # consume the first user message; then wait for approval
                "print(json.dumps({'type':'control_request','request_id':'no-answer',"
                "'request':{'tool_name':'Bash','input':{'command':'git status'}}}),flush=True)\n"
                "sys.stdin.readline()\n"
            )
            cli.chmod(0o700)
            record = Path(td) / "session.json"
            record.write_text(json.dumps({
                "executable": str(cli), "prompt": "review", "cwd": td,
                "session_id": "00000000-0000-4000-8000-000000000001",
                "options": {"duplex": True, "timeout_seconds": 2,
                            "approval_timeout_seconds": 1},
            }))
            worker = threading.Thread(target=run_worker, args=(str(record),), daemon=True)
            worker.start()
            worker.join(6)
            self.assertFalse(worker.is_alive(), "unanswered approval must have a bounded lifetime")
            receipt = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(receipt["status"], "STALLED", receipt)


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
                    a._path(sid).with_suffix(".events.jsonl").write_text(json.dumps({
                        "seq": 2, "kind": "permission_request", "request_id": "large",
                        "approvable": False, "input": {"truncated": True}}) + "\n")
                    with self.assertRaisesRegex(ValueError, "oversized"):
                        a.decide(sid, "large", "allow")


if __name__ == "__main__":
    unittest.main()
