from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import relay_agent


class EventBackoffTests(unittest.TestCase):
    def test_completion_batch_stops_after_first_unsubscribed_event(self) -> None:
        pending = [
            {
                "job_id": "job-1",
                "status": "SUCCEEDED",
                "completion_event": {
                    "event_id": "event-1",
                    "created_at": 1.0,
                },
            },
            {
                "job_id": "job-2",
                "status": "SUCCEEDED",
                "completion_event": {
                    "event_id": "event-2",
                    "created_at": 2.0,
                },
            },
        ]
        calls: list[str] = []

        def fake_request(base, path, body, token=None, timeout=35):
            calls.append(body["eventId"])
            return {"ok": True, "matching_subscriptions": 0, "delivered": 0}

        with patch.object(
            relay_agent.jobs,
            "pending_completion_events",
            return_value=pending,
        ), patch.object(relay_agent, "_request", fake_request):
            delivered = relay_agent._forward_completion_events(
                {"url": "https://relay.example", "device_token": "token"}
            )

        self.assertFalse(delivered)
        self.assertEqual(calls, ["event-1"])

    def test_cognition_batch_stops_after_first_undeliverable_event(self) -> None:
        pending = [
            {
                "request_id": "llmreq_1",
                "agent_id": "ferro",
                "created_at": 1.0,
                "activation_event": {
                    "event_id": "cognition-1",
                    "created_at": 1.0,
                },
            },
            {
                "request_id": "llmreq_2",
                "agent_id": "ferro",
                "created_at": 2.0,
                "activation_event": {
                    "event_id": "cognition-2",
                    "created_at": 2.0,
                },
            },
        ]
        calls: list[str] = []

        def fake_request(base, path, body, token=None, timeout=35):
            calls.append(body["eventId"])
            return {"ok": True, "matching_subscriptions": 1, "delivered": 0}

        with patch.object(
            relay_agent.cognition,
            "pending_activation_events",
            return_value=pending,
        ), patch.object(
            relay_agent.cognition,
            "dispatch_lane",
            return_value="default",
        ), patch.object(relay_agent, "_request", fake_request):
            delivered = relay_agent._forward_cognition_events(
                {"url": "https://relay.example", "device_token": "token"}
            )

        self.assertFalse(delivered)
        self.assertEqual(calls, ["cognition-1"])

    def test_task_completion_does_not_trigger_event_forwarding_inline(self) -> None:
        with patch.object(
            relay_agent,
            "_dispatch",
            return_value={"ok": True},
        ), patch.object(
            relay_agent,
            "_request",
            return_value={"ok": True},
        ) as request, patch.object(
            relay_agent,
            "_forward_events",
        ) as forward:
            relay_agent._handle_claimed_task(
                {"url": "https://relay.example", "device_token": "token"},
                {"task_id": "task-1", "tool": "connection_status", "args": {}},
            )

        self.assertEqual(request.call_count, 1)
        forward.assert_not_called()


if __name__ == "__main__":
    unittest.main()
