"""Local integration: native Pi diff -> Relay -> trusted GitHub writer inbox.

Exercises real Git blob identity and durable handoff; GitHub writes are mocked,
so no production, secrets, approvals or real PR status are changed.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "scripts"))
from eventgraph_pi_repair_completion import register, deliver
import eventgraph_pi_repair_completion as completion
from eventgraph_pi_repair_inbox import drain
from pi_repair_handoff import RepairHandoff
from store import RelayStore

SESSION = "lra_" + "a" * 32
CN = "e21c871f-317b-4f3e-95bf-8d4299547a40"
PATH = "packages/backend/src/editorial/explain.ts"


class PiPipelineTests(unittest.TestCase):
    def test_completed_local_diff_flows_once_to_trusted_writer_and_acks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RelayStore(str(root / "relay.sqlite"))
            board = RepairHandoff(store)
            worktrees = root / "worktrees"
            worktrees.mkdir()
            # Use a real Git commit, not a made-up blob SHA.
            checkout = worktrees / "seed"
            checkout.mkdir()
            def git(*argv):
                return subprocess.run(["git", "-C", str(checkout), *argv],
                    check=True, text=True, capture_output=True).stdout.strip()
            git("init", "-q")
            src = checkout / PATH
            src.parent.mkdir(parents=True)
            src.write_text("export const x = 1;\n")
            git("add", ".")
            git("-c", "user.email=test@example.invalid",
                "-c", "user.name=Test", "commit", "-qm", "seed")
            sha = git("rev-parse", "HEAD")
            number = 175
            target = worktrees / f"pi-repair-{number}-{sha[:12]}"
            checkout.rename(target)
            checkout = target
            (target / PATH).write_text("export const x = 2;\n")
            task = {"tool": "start_agent", "args": {
                "adapter_id": "pi", "project": "aihot-dev",
                "goal": f"[eventgraph-pi-repair pr={number} sha={sha}] Fix real bug"}}
            state = root / "journals"
            env = patch.dict(os.environ, {"LIVINGRUNTIME_PI_REPAIR_JOURNALS": str(state)})
            with env, patch.object(completion, "ROOT", worktrees), \
                 patch.object(completion.agent_sessions, "get", return_value={
                     "adapter_id": "pi", "project": "aihot-dev"}), \
                 patch.object(completion.agent_sessions, "status", return_value={
                     "session_id": SESSION, "agent_status": {"status": "SUCCEEDED"}}):
                board.record("owner", CN, number, sha, SESSION)
                register(task, {"ok": True, "result": {"session_id": SESSION}})
                def request(base, path, body, token, **kwargs):
                    if path == "/device/pi-repair-result":
                        board.submit("owner", CN, body["session_id"], body["manifest"])
                        return {"ok": True}
                    if path == "/device/pi-repair-inbox":
                        if body["action"] == "pending":
                            return {"items": board.pending("owner")}
                        board.acknowledge("owner", body["session_id"],
                                          body["original_sha"], body["new_sha"])
                        return {"ok": True}
                    self.fail("unrecognized Relay path " + path)
                self.assertTrue(deliver({"name": "cn-dev", "url": "https://test",
                                         "device_token": "device"}, request))
                self.assertFalse(deliver({"name": "cn-dev", "url": "https://test",
                                          "device_token": "device"}, request))
                items = board.pending("owner")
                self.assertEqual(len(items), 1)
                manifest = items[0]["manifest"]
                self.assertEqual(manifest["changes"][0]["content"], "export const x = 2;\n")
                self.assertEqual(manifest["changes"][0]["expected_blob_sha"],
                                 git("rev-parse", "HEAD:" + PATH))
                calls = []
                def writer(patch_manifest):
                    calls.append(patch_manifest)
                    return {"pr_number": number, "previous_sha": sha, "new_sha": "b" * 40}
                self.assertTrue(drain({"name": "owner-main", "url": "https://test",
                                       "device_token": "owner"}, request, writer))
                self.assertEqual(calls, [manifest])
                self.assertEqual(board.pending("owner"), [])


if __name__ == "__main__":
    unittest.main()
