"""Pi native subagent and SDK gateway regression tests."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pi_worker
import pi_tool_runtime
from livingruntime_adapter_pi import PiAdapter, validate_options
from agent_adapters import AgentCapabilities


class PiAdapterTests(unittest.TestCase):
    def test_options_and_capabilities(self):
        self.assertEqual(validate_options({"provider": "zai-coding-cn", "model": "glm-5.3"})["model"], "glm-5.3")
        with self.assertRaises(ValueError):
            validate_options({"shell": "rm -rf /"})
        with self.assertRaises(ValueError):
            validate_options({"tools": ["unknown"]})
        with self.assertRaises(ValueError):
            validate_options({"env_file": ""})
        self.assertEqual(PiAdapter().capabilities(), AgentCapabilities(
            resume=True, messaging=True, events=True, cancel=True))

    def test_worker_official_json_events_success_and_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "pi"
            fake.write_text('#!/usr/bin/env python3\n'
                'import json,sys\n'
                'print(json.dumps({"type":"agent_start"}),flush=True)\n'
                'print(json.dumps({"type":"tool_execution_start","toolName":"read","toolCallId":"1"}),flush=True)\n'
                'print(json.dumps({"type":"tool_execution_end","toolCallId":"1","isError":False}),flush=True)\n'
                'print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"finished"}]}}),flush=True)\n'
                'print(json.dumps({"type":"agent_settled"}),flush=True)\n', encoding="utf8")
            fake.chmod(0o700)
            sid = str(uuid.uuid4())
            record = root / (sid + ".json")
            record.write_text(json.dumps({
                "session_id": sid, "executable": str(fake), "cwd": td,
                "options": {"model": "glm-5.3", "provider": "zai-coding-cn"},
                "prompt": "test", "continue": False}), encoding="utf8")
            pi_worker.main(str(record))
            result = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertEqual(result["result"], "finished")
            events = [json.loads(line) for line in record.with_suffix(".events.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["kind"], "completion")
            self.assertIn("tool_call", [e["kind"] for e in events])
            self.assertEqual(events[0]["seq"], 1)
            # Simulate an accepted provider prompt that never settles.
            fake.write_text('#!/usr/bin/env python3\nprint("{}")\n', encoding="utf8")
            pi_worker.main(str(record))
            result = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(result["status"], "FAILED")
            events = [json.loads(line) for line in record.with_suffix(".events.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["seq"], len(events))

    def test_frozen_worker_dispatch(self):
        import connector
        with tempfile.TemporaryDirectory() as td:
            sid = str(uuid.uuid4())
            record = Path(td) / (sid + ".json")
            record.write_text(json.dumps({"session_id": sid}))
            with patch.dict(os.environ, {"LIVINGRUNTIME_PI_JOBS": td}), \
                 patch.object(pi_worker, "main") as run:
                connector.main(["agent-worker", "pi", str(record)])
                run.assert_called_once_with(str(record))

    def test_remote_bridge_invokes_real_pi_sdk_and_keeps_bash_in_exec_policy(self):
        import bridge
        with tempfile.TemporaryDirectory() as td, \
             patch.object(bridge, "_config", return_value={}), \
             patch.object(bridge, "resolve_path", return_value=td), \
             patch.object(bridge, "_transport", return_value="local"), \
             patch.object(bridge, "_audit"):
            made = bridge.pi_tool("write", {"path": "probe.txt", "content": "hello"}, project="demo")
            self.assertTrue(made.get("ok"), made)
            read = bridge.pi_tool("read", {"path": "probe.txt"}, project="demo")
            self.assertTrue(read.get("ok"), read)
            self.assertIn("hello", read["text"])
            with patch.object(bridge, "exec", return_value={"approval_required": True}) as protected:
                blocked = bridge.pi_tool("bash", {"argv": ["node", "--version"]}, project="demo")
            self.assertTrue(blocked["approval_required"])
            self.assertEqual(protected.call_args.args[0], ["node", "--version"])

    def test_optional_project_env_file_uses_node_without_embedding_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / ".env").write_text("NVIDIA_API_KEY=placeholder", encoding="utf8")
            data = {"executable": "/tmp/pi", "cwd":td, "prompt":"check",
                    "session_id":str(uuid.uuid4()), "options":{
                        "provider":"nvidia", "model":"z-ai/glm-5.3",
                        "env_file":str(home / ".env")}}
            args = pi_worker.cli_args(data)
            self.assertEqual(args[1], "--env-file=" + str(home / ".env"))
            self.assertNotIn("placeholder", " ".join(args))
            with patch("livingruntime_adapter_pi.pi_workspace", return_value=td), \
                 patch.object(PiAdapter, "_spawn", return_value="started"):
                fake = home / "pi"
                fake.touch()
                fake.chmod(0o700)
                adapter = PiAdapter(str(fake))
                self.assertEqual(adapter.start("test", project="p", device="main",
                      options={"provider":"nvidia","model":"z-ai/glm-5.3",
                               "env_file":".env"}), "started")
                with self.assertRaises(PermissionError):
                    adapter.start("test", project="p", device="main",
                                  options={"env_file":"../../etc/passwd"})

    def test_project_local_pi_executable_in_frozen_connector(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            executable = root / ".livingruntime/pi-harness/node_modules/.bin/pi"
            executable.parent.mkdir(parents=True)
            executable.write_text("fake-pi")
            executable.chmod(0o700)
            with patch("livingruntime_adapter_pi.pi_workspace", return_value=td), \
                 patch.dict(os.environ, {"LIVINGRUNTIME_PI_JOBS": str(root / "jobs")}), \
                 patch.object(PiAdapter, "_spawn", return_value="started"):
                adapter = PiAdapter("/missing/packed/pi")
                self.assertEqual(adapter.start("probe", project="p", device="main"), "started")
                records = list((root / "jobs").glob("*.json"))
                self.assertEqual(len(records), 1)
                self.assertEqual(json.loads(records[0].read_text())["executable"],
                                 str(executable.resolve()))

    def test_project_local_sdk_gateway_is_used(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gateway = root / ".livingruntime/pi-harness/pi_tool.mjs"
            gateway.parent.mkdir(parents=True)
            gateway.write_text("// sidecar")
            with patch.object(pi_tool_runtime.subprocess, "run") as run:
                run.return_value.stdout = '{"ok":true,"text":"ok"}'
                run.return_value.returncode = 0
                result = pi_tool_runtime.execute("read", {"path":"README.md"}, cwd=td)
            self.assertTrue(result["ok"])
            self.assertEqual(run.call_args.args[0][1], str(gateway))

    def test_direct_tool_rejects_bash(self):
        with self.assertRaisesRegex(ValueError, "bash uses Remote exec"):
            pi_tool_runtime.execute("bash", {"command":"echo a"}, cwd="/tmp")


if __name__ == "__main__":
    unittest.main()
