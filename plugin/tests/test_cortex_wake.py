from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cortex_wake  # noqa: E402


class CortexWakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        self.repo = root / "wake"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "init", str(self.repo)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        (self.repo / "README.md").write_text("wake bus\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "base"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-b", "livingruntime/ferro-cortex-wake"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        (self.repo / "FERRO_CORTEX_WAKE.json").write_text(
            json.dumps(
                {
                    "schema_version": "livingruntime-cortex-wake.v1",
                    "agent_id": "ferro",
                    "request_id": None,
                    "purpose": "bootstrap",
                    "wake_sequence": 0,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "FERRO_CORTEX_WAKE.json"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "wake bus"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", str(self.remote)],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "push",
                "-u",
                "origin",
                "livingruntime/ferro-cortex-wake",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.env = patch.dict(
            os.environ,
            {
                "LIVINGRUNTIME_COGNITION_GITHUB_WAKE_REPO": str(self.repo),
                "LIVINGRUNTIME_COGNITION_GITHUB_WAKE_BRANCH": "livingruntime/ferro-cortex-wake",
                "LIVINGRUNTIME_COGNITION_GITHUB_WAKE_AGENT": "ferro",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def request(self, request_id: str = "llmreq_test") -> dict:
        return {
            "request_id": request_id,
            "agent_id": "ferro",
            "purpose": "content_cognition",
            "created_at": 123.0,
            "payload_sha256": "abc123",
            "messages": [{"role": "user", "content": "private prompt must never reach GitHub"}],
            "metadata": {"secret_context": "private"},
        }

    def test_emit_pushes_only_bounded_wake_marker(self) -> None:
        result = cortex_wake.emit(self.request())
        self.assertEqual(result["status"], "EMITTED")
        self.assertEqual(result["wake_sequence"], 1)

        marker = json.loads(
            (self.repo / "FERRO_CORTEX_WAKE.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["event"], "FERRO_CORTEX_WAKE")
        self.assertEqual(marker["request_id"], "llmreq_test")
        self.assertEqual(marker["purpose"], "content_cognition")
        self.assertNotIn("messages", marker)
        self.assertNotIn("metadata", marker)

        remote_marker = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "show",
                "livingruntime/ferro-cortex-wake:FERRO_CORTEX_WAKE.json",
            ],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        self.assertEqual(json.loads(remote_marker)["request_id"], "llmreq_test")

    def test_emit_is_idempotent_for_same_request(self) -> None:
        first = cortex_wake.emit(self.request())
        count_before = subprocess.run(
            ["git", "-C", str(self.repo), "rev-list", "--count", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        second = cortex_wake.emit(self.request())
        count_after = subprocess.run(
            ["git", "-C", str(self.repo), "rev-list", "--count", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.assertEqual(first["status"], "EMITTED")
        self.assertEqual(second["status"], "ALREADY_EMITTED")
        self.assertEqual(count_before, count_after)

    def test_other_agents_do_not_ring_ferro_bus(self) -> None:
        request = self.request()
        request["agent_id"] = "other-agent"
        result = cortex_wake.emit(request)
        self.assertEqual(result["status"], "IGNORED_AGENT")

    def test_unconfigured_bridge_is_explicitly_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {"LIVINGRUNTIME_COGNITION_GITHUB_WAKE_REPO": ""},
            clear=False,
        ):
            result = cortex_wake.emit(self.request())
        self.assertEqual(result["status"], "DISABLED")


if __name__ == "__main__":
    unittest.main()
