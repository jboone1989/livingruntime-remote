from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import relay_agent


class RelayAgentPollingTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        path = root / "relay.json"
        path.write_text(
            '{"url":"https://relay.example.test","device_id":"dev","device_token":"token"}\n',
            encoding="utf-8",
        )
        return path

    def test_once_polls_and_handles_task_before_forwarding_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            calls: list[str] = []

            def fake_request(base, path, body, token=None, timeout=35):
                calls.append(path)
                self.assertEqual(path, "/device/poll?wait=20")
                return {
                    "task": {
                        "task_id": "task-1",
                        "tool": "connection_status",
                        "args": {},
                    }
                }

            def fake_handle(cfg, task):
                calls.append("handle:" + task["task_id"])

            def forbidden_forward(cfg):
                raise AssertionError("event forwarding ran before the task lane")

            with patch.object(relay_agent, "_request", fake_request), patch.object(
                relay_agent, "_handle_claimed_task", fake_handle
            ), patch.object(relay_agent, "_forward_events", forbidden_forward):
                relay_agent.serve(config, once=True)

            self.assertEqual(
                calls,
                ["/device/poll?wait=20", "handle:task-1"],
            )

    def test_blocked_event_forwarder_does_not_starve_next_task_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            forward_started = threading.Event()
            release_forward = threading.Event()
            poll_count = 0

            def blocked_forward(cfg):
                forward_started.set()
                self.assertTrue(
                    release_forward.wait(timeout=2),
                    "test did not release event forwarder",
                )

            def fake_request(base, path, body, token=None, timeout=35):
                nonlocal poll_count
                self.assertEqual(path, "/device/poll?wait=20")
                poll_count += 1
                if poll_count == 1:
                    return {"task": None}
                self.assertTrue(
                    forward_started.wait(timeout=1),
                    "event worker was not started after first poll",
                )
                release_forward.set()
                raise KeyboardInterrupt

            with patch.object(relay_agent, "_request", fake_request), patch.object(
                relay_agent, "_forward_events", blocked_forward
            ):
                relay_agent.serve(config)

            self.assertEqual(poll_count, 2)


if __name__ == "__main__":
    unittest.main()
