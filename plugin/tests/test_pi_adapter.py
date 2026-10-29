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
            workspace = home / "workspace"
            workspace.mkdir()
            (workspace / ".env").write_text("NVIDIA_API_KEY=placeholder", encoding="utf8")
            data = {"executable": "/tmp/pi", "cwd":str(workspace), "prompt":"check",
                    "session_id":str(uuid.uuid4()), "options":{
                        "provider":"nvidia", "model":"z-ai/glm-5.3",
                        "env_file":str(workspace / ".env")}}
            args = pi_worker.cli_args(data)
            self.assertEqual(args[1], "--env-file=" + str(workspace / ".env"))
            self.assertNotIn("placeholder", " ".join(args))
            with patch("livingruntime_adapter_pi.pi_workspace", return_value=str(workspace)), \
                 patch.object(PiAdapter, "_spawn", return_value="started"):
                fake = home / "pi"
                fake.touch()
                fake.chmod(0o700)
                adapter = PiAdapter(str(fake))
                self.assertEqual(adapter.start("test", project="p", device="main",
                      options={"provider":"nvidia","model":"z-ai/glm-5.3",
                               "env_file":".env"}), "started")
                outside = home / "outside.env"
                outside.write_text("NVIDIA_API_KEY=test", encoding="utf8")
                with self.assertRaises(PermissionError):
                    adapter.start("test", project="p", device="main",
                                  options={"env_file":str(outside)})

    def test_default_routes_bigmodel_official_with_owner_home_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            (workspace / ".livingruntime").mkdir(parents=True)
            (workspace / ".livingruntime/official-glm.env").write_text("BIGMODEL_API_KEY=untrusted")
            home_config = root / ".livingruntime"
            home_config.mkdir()
            env_file = home_config / "official-glm.env"
            env_file.write_text("BIGMODEL_API_KEY=placeholder", encoding="utf-8")
            fake = root / "pi"
            fake.write_text("# stub")
            with patch("livingruntime_adapter_pi.pi_workspace", return_value=str(workspace)), \
                 patch.dict(os.environ, {"HOME":td,"LIVINGRUNTIME_PI_JOBS": str(root / "jobs")}), \
                 patch.object(PiAdapter, "_spawn", return_value="started"):
                adapter = PiAdapter(str(fake))
                self.assertEqual(adapter.start("probe", project="p", device="main"), "started")
                records = list((root / "jobs").glob("*.json"))
                self.assertEqual(len(records), 1)
                opts = json.loads(records[0].read_text())["options"]
                self.assertEqual(opts["provider"], "bigmodel-official")
                self.assertEqual(opts["model"], "glm-5.3")
                self.assertEqual(opts["env_file"], str(env_file.resolve()))
                self.assertNotIn("placeholder", json.dumps(opts))
                rec = json.loads(records[0].read_text())
                agent_dir = Path(rec["agent_dir"])
                self.assertEqual(agent_dir, home_config / "pi-harness/agent-config")
                settings = json.loads((agent_dir / "models.json").read_text())
                official = settings["providers"]["bigmodel-official"]
                self.assertEqual(official["baseUrl"], "https://open.bigmodel.cn/api/paas/v4")
                self.assertEqual(official["api"], "openai-completions")
                self.assertEqual(official["apiKey"], "BIGMODEL_API_KEY")
                self.assertEqual(official["models"][0]["id"], "glm-5.3")
                self.assertNotIn("placeholder", json.dumps(settings))
                self.assertFalse((workspace / ".pi/agent/models.json").exists())

    def test_worker_injects_trusted_agent_dir_only_into_native_pi_child(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent_dir = root / "trusted-models"
            agent_dir.mkdir()
            fake = root / "pi"
            fake.write_text('#!/usr/bin/env python3\n'
                'import json,os\n'
                'print(json.dumps({"type":"message_end","message":{"role":"assistant",'
                '"content":[{"type":"text","text":os.environ.get("PI_CODING_AGENT_DIR","")}]}}))\n'
                'print(json.dumps({"type":"agent_settled"}))\n', encoding="utf-8")
            fake.chmod(0o700)
            record = root / (str(uuid.uuid4()) + ".json")
            record.write_text(json.dumps({
                "session_id": str(uuid.uuid4()), "executable": str(fake),
                "cwd": td, "agent_dir": str(agent_dir), "prompt": "test",
                "options": {"provider": "bigmodel-official", "model": "glm-5.3"},
            }), encoding="utf-8")
            pi_worker.main(str(record))
            result = json.loads(record.with_suffix(".result.json").read_text())
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertEqual(result["result"], str(agent_dir))

    def test_default_prefers_trusted_home_before_project_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            rogue = workspace / "pi"
            rogue.write_text("untrusted")
            trusted = root / ".livingruntime/pi-harness/node_modules/.bin/pi"
            trusted.parent.mkdir(parents=True)
            trusted.write_text("trusted")
            with patch.dict(os.environ, {"HOME": td, "LIVINGRUNTIME_PI_EXECUTABLE": "",
                                        "LIVINGRUNTIME_PI_JOBS": str(root / "jobs")}), \
                 patch("livingruntime_adapter_pi.shutil.which", return_value=str(rogue)), \
                 patch("livingruntime_adapter_pi.pi_workspace", return_value=str(workspace)), \
                 patch.object(PiAdapter, "_spawn", return_value="started"):
                adapter = PiAdapter()
                self.assertEqual(adapter.executable, str(trusted.resolve()))
                self.assertEqual(adapter.start("probe", project="p", device="main"), "started")

    def test_trusted_pi_executable_in_frozen_connector(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            executable = root / ".livingruntime/pi-harness/node_modules/.bin/pi"
            executable.parent.mkdir(parents=True)
            executable.write_text("fake-pi")
            executable.chmod(0o700)
            with patch("livingruntime_adapter_pi.pi_workspace", return_value=str(workspace)), \
                 patch.dict(os.environ, {"HOME": td, "LIVINGRUNTIME_PI_JOBS": str(root / "jobs")}), \
                 patch.object(PiAdapter, "_spawn", return_value="started"):
                adapter = PiAdapter("/missing/packed/pi")
                self.assertEqual(adapter.start("probe", project="p", device="main"), "started")
                records = list((root / "jobs").glob("*.json"))
                self.assertEqual(len(records), 1)
                self.assertEqual(json.loads(records[0].read_text())["executable"],
                                 str(executable.resolve()))
                rogue = workspace / "pi"
                rogue.write_text("agent supplied executable")
                with self.assertRaises(PermissionError):
                    PiAdapter(str(rogue)).start("probe", project="p", device="main")

    def test_owner_home_sdk_gateway_is_used(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            gateway = root / ".livingruntime/pi-harness/pi_tool.mjs"
            gateway.parent.mkdir(parents=True)
            gateway.write_text("// trusted sidecar")
            rogue = workspace / ".livingruntime/pi-harness/pi_tool.mjs"
            rogue.parent.mkdir(parents=True)
            rogue.write_text("// agent supplied JS, never execute")
            with patch.dict(os.environ, {"HOME":td}), \
                 patch.object(pi_tool_runtime.subprocess, "run") as run:
                run.return_value.stdout = '{"ok":true,"text":"ok"}'
                run.return_value.returncode = 0
                result = pi_tool_runtime.execute("read", {"path":"README.md"}, cwd=str(workspace))
            self.assertTrue(result["ok"])
            self.assertEqual(run.call_args.args[0][1], str(gateway))

    def test_followup_does_not_show_previous_turn_result(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.dict(os.environ, {"LIVINGRUNTIME_PI_JOBS": str(Path(td) / "jobs")}):
            fake = Path(td) / "pi"
            fake.touch()
            adapter = PiAdapter(str(fake))
            sid = str(uuid.uuid4())
            adapter._write(sid, {"session_id": sid, "status": "SUCCEEDED",
                                "result": "OLD_TURN_RESULT", "finished_at": 123.0,
                                "cwd": td, "options": {}})
            with patch("livingruntime_adapter_claude.subprocess.Popen") as spawned:
                spawned.return_value.pid = 24680
                adapter._spawn(sid, "Continue", True)
            record = adapter._read(sid)
            self.assertEqual(record["status"], "RUNNING")
            self.assertNotIn("result", record)
            self.assertNotIn("finished_at", record)

    def test_direct_tool_rejects_bash(self):
        with self.assertRaisesRegex(ValueError, "bash uses Remote exec"):
            pi_tool_runtime.execute("bash", {"command":"echo a"}, cwd="/tmp")


if __name__ == "__main__":
    unittest.main()
