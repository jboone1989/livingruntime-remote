from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cognition  # noqa: E402
import cognition_cli  # noqa: E402
import cognitionctl  # noqa: E402


class CognitionQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "cognition"
        self.env = patch.dict(
            os.environ,
            {
                "LIVINGRUNTIME_COGNITION_ROOT": str(self.root),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def submit(self, request_id: str | None = None, *, agent_id: str = "ferro"):
        return cognition.submit(
            agent_id=agent_id,
            purpose="strategy_planning",
            messages=[
                {"role": "system", "content": "Reason carefully."},
                {"role": "user", "content": "Choose the next action."},
            ],
            response_format={"type": "text"},
            options={"max_output_tokens": 800},
            metadata={"goal_id": "goal-1"},
            timeout_seconds=300,
            request_id=request_id,
        )

    def test_submit_is_durable_private_and_idempotent(self) -> None:
        first = self.submit("llmreq_fixed")
        second = self.submit("llmreq_fixed")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(first["status"], "PENDING")
        path = self.root / "requests" / "llmreq_fixed.json"
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        event = first["activation_event"]
        self.assertTrue(event["event_id"].startswith("lrcog_"))
        self.assertIsNone(event["event_forwarded_at"])
        pending = cognition.pending_activation_events()
        self.assertEqual([row["request_id"] for row in pending], ["llmreq_fixed"])
        cognition.mark_activation_event_forwarded(
            "llmreq_fixed",
            event["event_id"],
            forwarded_at=1234.0,
        )
        self.assertEqual(cognition.pending_activation_events(), [])
        stored = cognition.get("llmreq_fixed")
        self.assertEqual(stored["activation_event"]["event_forwarded_at"], 1234.0)

    def test_submit_only_emits_activation_for_pending_request(self) -> None:
        first = self.submit("llmreq_activation")
        claimed = cognition.claim_request(
            request_id="llmreq_activation",
            watcher_id="watcher_activation",
            claim_seconds=30,
        )
        cognition.complete(
            request_id="llmreq_activation",
            response_text="done",
            claim_token=claimed["claim"]["token"],
        )
        repeated = self.submit("llmreq_activation")

        self.assertEqual(first["activation"]["status"], "QUEUED")
        self.assertEqual(first["activation"]["transport"], "mcp-event")
        self.assertTrue(first["activation"]["event_id"].startswith("lrcog_"))
        self.assertEqual(repeated["status"], "COMPLETED")
        self.assertEqual(repeated["activation"]["status"], "NOT_REQUIRED")

    def test_rearm_pending_only_reemits_while_request_is_pending(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            created = self.submit("llmreq_rearm_pending")
        original_event = created["activation_event"]["event_id"]
        with patch.object(cognition.time, "time", return_value=1030.0):
            pending = cognition.rearm_pending(
                "llmreq_rearm_pending",
                min_interval_seconds=60,
            )
        with patch.object(cognition.time, "time", return_value=1061.0):
            rearmed = cognition.rearm_pending(
                "llmreq_rearm_pending",
                min_interval_seconds=60,
            )
        self.assertNotEqual(rearmed["activation_event"]["event_id"], original_event)
        self.assertIsNone(rearmed["activation_event"]["event_forwarded_at"])
        with patch.object(cognition.time, "time", return_value=1062.0):
            claimed = cognition.claim_request(
                request_id="llmreq_rearm_pending",
                watcher_id="watcher_rearm",
                claim_seconds=30,
            )
            dispatched = cognition.rearm_pending(
                "llmreq_rearm_pending",
                min_interval_seconds=60,
            )
            cognition.complete(
                request_id="llmreq_rearm_pending",
                response_text="done",
                claim_token=claimed["claim"]["token"],
            )
            completed = cognition.rearm_pending(
                "llmreq_rearm_pending",
                min_interval_seconds=60,
            )

        self.assertEqual(created["status"], "PENDING")
        self.assertEqual(pending["activation"]["status"], "COOLDOWN")
        self.assertEqual(rearmed["activation"]["status"], "QUEUED")
        self.assertEqual(rearmed["activation"]["transport"], "mcp-event")
        self.assertEqual(dispatched["activation"]["status"], "NOT_REQUIRED")
        self.assertEqual(dispatched["activation"]["request_status"], "DISPATCHED")
        self.assertEqual(completed["activation"]["status"], "NOT_REQUIRED")
        self.assertEqual(completed["activation"]["request_status"], "COMPLETED")

    def test_cognitionctl_get_can_explicitly_rearm_pending_request(self) -> None:
        rearmed = {
            "request_id": "llmreq_cli_rearm",
            "status": "PENDING",
            "activation": {"status": "QUEUED", "transport": "mcp-event"},
        }
        with patch.object(
            sys,
            "argv",
            [
                "cognitionctl.py",
                "get",
                "llmreq_cli_rearm",
                "--rearm-pending",
                "--rearm-min-interval-seconds",
                "45",
            ],
        ), patch.object(
            cognitionctl,
            "rearm_pending",
            return_value=rearmed,
        ) as rearm_mock, patch.object(
            cognitionctl,
            "_print",
        ) as print_mock:
            self.assertEqual(cognitionctl.main(), 0)

        rearm_mock.assert_called_once_with(
            "llmreq_cli_rearm",
            min_interval_seconds=45,
        )
        print_mock.assert_called_once_with(rearmed)

    def test_request_id_conflict_is_rejected(self) -> None:
        self.submit("llmreq_conflict")
        with self.assertRaisesRegex(RuntimeError, "idempotency conflict"):
            cognition.submit(
                agent_id="ferro",
                purpose="different",
                messages=[{"role": "user", "content": "different"}],
                request_id="llmreq_conflict",
            )

    def test_widget_wait_is_read_only_until_explicit_claim(self) -> None:
        self.submit("llmreq_widget")

        waited = cognition.wait_pending(agent_id="ferro", timeout_seconds=1)
        self.assertFalse(waited["timed_out"])
        self.assertEqual(waited["request"]["request_id"], "llmreq_widget")
        stored = json.loads(
            (self.root / "requests" / "llmreq_widget.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["status"], "PENDING")
        self.assertEqual(stored["attempts"], 0)
        self.assertIsNone(stored["claim"])

        claimed = cognition.claim_request(
            request_id="llmreq_widget",
            watcher_id="watcher_widget",
            claim_seconds=30,
        )
        self.assertEqual(claimed["status"], "DISPATCHED")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(claimed["claim"]["watcher_id"], "watcher_widget")
        self.assertTrue(claimed["claim"]["token"])

    def test_claim_serializes_agent_and_completion_records_provenance(self) -> None:
        first = self.submit("llmreq_one")
        self.submit("llmreq_two")

        claimed = cognition.claim_next(agent_id="ferro", watcher_id="watcher_1")
        self.assertEqual(claimed["request_id"], first["request_id"])
        self.assertEqual(claimed["status"], "DISPATCHED")
        token = claimed["claim"]["token"]

        self.assertIsNone(cognition.claim_next(agent_id="ferro", watcher_id="watcher_2"))

        with self.assertRaisesRegex(RuntimeError, "claim token mismatch"):
            cognition.complete(
                request_id=first["request_id"],
                response_text="answer",
                claim_token="wrong",
            )

        done = cognition.complete(
            request_id=first["request_id"],
            response_text="answer",
            claim_token=token,
            model="gpt-5.6-sol",
            session_id="session-test",
        )
        self.assertEqual(done["status"], "COMPLETED")
        self.assertEqual(done["response"]["provider"], "livingruntime-chatgpt")
        self.assertEqual(done["response"]["model"], "gpt-5.6-sol")
        self.assertEqual(done["response"]["session_id"], "session-test")
        self.assertEqual(done["response"]["tool_calls"], [])
        digest_payload = json.dumps(
            {"text": "answer", "tool_calls": []},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            done["response"]["sha256"],
            hashlib.sha256(digest_payload).hexdigest(),
        )

        second = cognition.claim_next(agent_id="ferro", watcher_id="watcher_2")
        self.assertEqual(second["request_id"], "llmreq_two")

    def test_dispatch_lanes_allow_public_text_while_owner_dialogue_is_running(self) -> None:
        owner = cognition.submit(
            agent_id="ferro",
            purpose="owner_dialogue",
            messages=[{"role": "user", "content": "long owner turn"}],
            metadata={"routing_task_class": "owner_dialogue"},
            request_id="llmreq_owner_lane",
        )
        public = cognition.submit(
            agent_id="ferro",
            purpose="simple_public_text",
            messages=[{"role": "user", "content": "latency sensitive visitor"}],
            request_id="llmreq_public_lane",
        )

        owner_claim = cognition.claim_request(
            request_id=owner["request_id"],
            watcher_id="watcher_owner",
            claim_seconds=300,
        )
        public_claim = cognition.claim_request(
            request_id=public["request_id"],
            watcher_id="watcher_public",
            claim_seconds=300,
        )

        self.assertEqual(cognition.dispatch_lane(owner_claim), "owner")
        self.assertEqual(cognition.dispatch_lane(public_claim), "public")
        self.assertEqual(owner_claim["status"], "DISPATCHED")
        self.assertEqual(public_claim["status"], "DISPATCHED")

    def test_same_dispatch_lane_remains_serialized(self) -> None:
        first = cognition.submit(
            agent_id="ferro",
            purpose="owner_dialogue",
            messages=[{"role": "user", "content": "first"}],
            metadata={"routing_task_class": "owner_dialogue"},
            request_id="llmreq_owner_lane_first",
        )
        second = cognition.submit(
            agent_id="ferro",
            purpose="owner_dialogue",
            messages=[{"role": "user", "content": "second"}],
            metadata={"routing_task_class": "owner_dialogue"},
            request_id="llmreq_owner_lane_second",
        )
        cognition.claim_request(
            request_id=first["request_id"],
            watcher_id="watcher_owner_first",
            claim_seconds=300,
        )

        with self.assertRaisesRegex(RuntimeError, "lane owner"):
            cognition.claim_request(
                request_id=second["request_id"],
                watcher_id="watcher_owner_second",
                claim_seconds=300,
            )

    def test_claim_next_skips_busy_lane_and_claims_highest_free_lane(self) -> None:
        owner = cognition.submit(
            agent_id="ferro",
            purpose="owner_dialogue",
            messages=[{"role": "user", "content": "owner"}],
            metadata={"routing_task_class": "owner_dialogue"},
            request_id="llmreq_busy_owner",
        )
        cognition.submit(
            agent_id="ferro",
            purpose="self_repair",
            messages=[{"role": "user", "content": "repair"}],
            metadata={"routing_task_class": "self_repair"},
            request_id="llmreq_free_repair",
        )
        cognition.claim_request(
            request_id=owner["request_id"],
            watcher_id="watcher_owner_busy",
            claim_seconds=300,
        )

        claimed = cognition.claim_next(
            agent_id="ferro",
            watcher_id="watcher_repair",
        )

        self.assertEqual(claimed["request_id"], "llmreq_free_repair")
        self.assertEqual(cognition.dispatch_lane(claimed), "repair")

    def test_lane_scoped_wait_does_not_attach_to_other_active_lane(self) -> None:
        owner = cognition.submit(
            agent_id="ferro",
            purpose="owner_dialogue",
            messages=[{"role": "user", "content": "owner"}],
            metadata={"routing_task_class": "owner_dialogue"},
            request_id="llmreq_active_owner",
        )
        public = cognition.submit(
            agent_id="ferro",
            purpose="simple_public_text",
            messages=[{"role": "user", "content": "public"}],
            request_id="llmreq_wait_public",
        )
        cognition.claim_request(
            request_id=owner["request_id"],
            watcher_id="chatgpt-work-ferro-cortex",
            claim_seconds=300,
        )

        waited = cognition.wait_pending(
            agent_id="ferro",
            watcher_id="chatgpt-work-ferro-cortex",
            timeout_seconds=1,
            lane="public",
        )

        self.assertFalse(waited["timed_out"])
        self.assertEqual(waited["request"]["request_id"], public["request_id"])
        self.assertEqual(waited["request"]["dispatch_lane"], "public")

    def test_priority_prefers_self_repair_over_older_background_cognition(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="general_text",
                messages=[{"role": "user", "content": "WORLD_SCAN_PLAN"}],
                metadata={"source": "ferro", "routing_task_class": "general_text"},
                request_id="llmreq_background_old",
            )
        with patch.object(cognition.time, "time", return_value=1001.0):
            cognition.submit(
                agent_id="ferro",
                purpose="self_repair",
                messages=[{"role": "user", "content": "GENERATE_BOUNDED_SELF_REPAIR_PLAN"}],
                metadata={"source": "ferro", "routing_task_class": "self_repair"},
                request_id="llmreq_repair_new",
            )

        with patch.object(cognition.time, "time", return_value=1002.0):
            claimed = cognition.claim_next(
                agent_id="ferro",
                watcher_id="watcher_priority",
            )

        self.assertEqual(claimed["request_id"], "llmreq_repair_new")

    def test_priority_recognizes_legacy_harness_rsi_purpose_without_metadata(self) -> None:
        cognition.submit(
            agent_id="ferro",
            purpose="owner_dialogue",
            messages=[{"role": "user", "content": "advance owner goal"}],
            metadata={"source": "ferro", "routing_task_class": "owner_dialogue"},
            request_id="llmreq_owner_waiting",
        )
        cognition.submit(
            agent_id="ferro",
            purpose="ferro.harness-rsi.propose",
            messages=[{"role": "user", "content": "repair harness"}],
            metadata={"parent_repair_request_id": "repair-legacy"},
            request_id="llmreq_legacy_rrsi",
        )

        peeked = cognition.peek_next(agent_id="ferro")

        self.assertEqual(peeked["request_id"], "llmreq_legacy_rrsi")


    def test_priority_prefers_owner_dialogue_over_older_background_cognition(self) -> None:
        cognition.submit(
            agent_id="ferro",
            purpose="general_text",
            messages=[{"role": "user", "content": "background"}],
            metadata={"source": "ferro", "routing_task_class": "general_text"},
            request_id="llmreq_background",
        )
        cognition.submit(
            agent_id="ferro",
            purpose="owner_dialogue",
            messages=[{"role": "user", "content": "advance owner goal"}],
            metadata={"source": "ferro", "routing_task_class": "owner_dialogue"},
            request_id="llmreq_owner_dialogue",
        )

        peeked = cognition.peek_next(agent_id="ferro")

        self.assertEqual(peeked["request_id"], "llmreq_owner_dialogue")

    def test_ancient_expired_dispatched_text_request_is_not_reclaimed(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="general_text",
                messages=[{"role": "user", "content": "old dispatched"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=0,
                request_id="llmreq_ancient_dispatched",
            )
            claimed = cognition.claim_request(
                request_id="llmreq_ancient_dispatched",
                watcher_id="watcher_old",
                claim_seconds=15,
            )
        path = self.root / "requests" / "llmreq_ancient_dispatched.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["deadline_at"] = None
        stored["response_deadline_at"] = None
        path.write_text(json.dumps(stored), encoding="utf-8")

        stale_at = 1000.0 + cognition.MAX_UNCLAIMED_DURABLE_AGE_SECONDS
        with patch.object(cognition.time, "time", return_value=stale_at + 1):
            peeked = cognition.peek_next(agent_id="ferro")
            expired = cognition.get("llmreq_ancient_dispatched")

        self.assertEqual(claimed["status"], "DISPATCHED")
        self.assertIsNone(peeked)
        self.assertEqual(expired["status"], "TIMED_OUT")
        self.assertEqual(expired["timeout_phase"], "DISPATCH_STALE")

    def test_ancient_dispatched_request_with_live_claim_is_preserved(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="general_text",
                messages=[{"role": "user", "content": "still owned"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=0,
                request_id="llmreq_live_old_claim",
            )
        stale_at = 1000.0 + cognition.MAX_UNCLAIMED_DURABLE_AGE_SECONDS
        path = self.root / "requests" / "llmreq_live_old_claim.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["status"] = "DISPATCHED"
        stored["attempts"] = 1
        stored["claim"] = {
            "watcher_id": "watcher_live",
            "token": "live-token",
            "claimed_at": stale_at,
            "expires_at": stale_at + 300,
        }
        stored["deadline_at"] = None
        path.write_text(json.dumps(stored), encoding="utf-8")

        with patch.object(cognition.time, "time", return_value=stale_at + 1):
            same = cognition.peek_next(
                agent_id="ferro",
                watcher_id="watcher_live",
            )
            preserved = cognition.get("llmreq_live_old_claim")

        self.assertEqual(preserved["status"], "DISPATCHED")
        self.assertEqual(same["request_id"], "llmreq_live_old_claim")
        self.assertTrue(same["owned_by_watcher"])

    def test_stale_claim_can_be_reclaimed(self) -> None:
        self.submit("llmreq_reclaim")
        with patch.object(cognition.time, "time", return_value=1000.0):
            claimed = cognition.claim_next(
                agent_id="ferro",
                watcher_id="watcher_old",
                claim_seconds=15,
            )
        old_token = claimed["claim"]["token"]

        with patch.object(cognition.time, "time", return_value=1020.0):
            reclaimed = cognition.claim_next(
                agent_id="ferro",
                watcher_id="watcher_new",
                claim_seconds=15,
            )
        self.assertEqual(reclaimed["request_id"], "llmreq_reclaim")
        self.assertNotEqual(reclaimed["claim"]["token"], old_token)
        self.assertEqual(reclaimed["claim"]["watcher_id"], "watcher_new")
        self.assertEqual(reclaimed["attempts"], 2)

    def test_deadline_moves_request_to_timed_out(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            created = cognition.submit(
                agent_id="ferro",
                purpose="timeout",
                messages=[{"role": "user", "content": "hello"}],
                timeout_seconds=5,
                request_id="llmreq_timeout",
            )
        self.assertEqual(created["deadline_at"], 1005.0)
        with patch.object(cognition.time, "time", return_value=1006.0):
            timed_out = cognition.get("llmreq_timeout")
        self.assertEqual(timed_out["status"], "TIMED_OUT")
        self.assertEqual(timed_out["finished_at"], 1006.0)

    def test_unclaimed_durable_request_survives_dispatch_grace(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            created = cognition.submit(
                agent_id="ferro",
                purpose="interactive-fallback",
                messages=[{"role": "user", "content": "hello"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=15,
                request_id="llmreq_dispatch_grace",
            )
        self.assertIsNone(created["deadline_at"])
        self.assertEqual(created["dispatch_deadline_at"], 1015.0)
        self.assertEqual(created["timeout_seconds"], 300)

        with patch.object(cognition.time, "time", return_value=1016.0):
            waiting = cognition.get("llmreq_dispatch_grace")
        self.assertEqual(waiting["status"], "PENDING")
        self.assertEqual(waiting["dispatch_waiting_since"], 1015.0)
        self.assertIsNone(waiting["deadline_at"])
        self.assertIsNone(waiting["timeout_phase"])

    def test_unclaimed_durable_request_eventually_times_out_as_stale_dispatch(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="general_text",
                messages=[{"role": "user", "content": "hello"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=15,
                request_id="llmreq_dispatch_stale",
            )
        stale_at = 1000.0 + cognition.MAX_UNCLAIMED_DURABLE_AGE_SECONDS
        with patch.object(cognition.time, "time", return_value=stale_at + 1):
            expired = cognition.get("llmreq_dispatch_stale")
        self.assertEqual(expired["status"], "TIMED_OUT")
        self.assertEqual(expired["timeout_phase"], "DISPATCH_STALE")
        self.assertEqual(expired["finished_at"], stale_at + 1)

    def test_interactive_request_preempts_older_pending_background_backlog(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="general_text",
                messages=[{"role": "user", "content": "old background work"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=15,
                request_id="llmreq_old_background",
            )
        with patch.object(cognition.time, "time", return_value=1001.0):
            cognition.submit(
                agent_id="ferro",
                purpose="simple_public_text",
                messages=[{"role": "user", "content": "live visitor"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=20,
                request_id="llmreq_live_chat",
            )
            peeked = cognition.peek_next(agent_id="ferro")
            claimed = cognition.claim_next(
                agent_id="ferro",
                watcher_id="watcher_live_chat",
            )
            cognition.complete(
                request_id=claimed["request_id"],
                response_text="live reply",
                claim_token=claimed["claim"]["token"],
            )
            background = cognition.claim_next(
                agent_id="ferro",
                watcher_id="watcher_background",
            )

        self.assertEqual(peeked["request_id"], "llmreq_live_chat")
        self.assertEqual(claimed["request_id"], "llmreq_live_chat")
        self.assertEqual(background["request_id"], "llmreq_old_background")

    def test_legacy_unbounded_unclaimed_request_expires_after_stale_cap(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            created = cognition.submit(
                agent_id="ferro",
                purpose="general_text",
                messages=[{"role": "user", "content": "legacy"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=0,
                request_id="llmreq_legacy_stale",
            )
        path = self.root / "requests" / "llmreq_legacy_stale.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["dispatch_timeout_seconds"] = None
        stored["dispatch_deadline_at"] = None
        stored["deadline_at"] = None
        path.write_text(json.dumps(stored), encoding="utf-8")

        stale_at = 1000.0 + cognition.MAX_UNCLAIMED_DURABLE_AGE_SECONDS
        with patch.object(cognition.time, "time", return_value=stale_at + 1):
            expired = cognition.get("llmreq_legacy_stale")

        self.assertEqual(created["status"], "PENDING")
        self.assertEqual(expired["status"], "TIMED_OUT")
        self.assertEqual(expired["timeout_phase"], "DISPATCH_STALE")

    def test_old_zero_dispatch_general_text_is_migrated_out_of_backlog(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="general_text",
                messages=[{"role": "user", "content": "old general text"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=0,
                request_id="llmreq_old_zero_general",
            )
        stale_at = 1000.0 + cognition.MAX_UNCLAIMED_DURABLE_AGE_SECONDS
        with patch.object(cognition.time, "time", return_value=stale_at + 1):
            expired = cognition.get("llmreq_old_zero_general")
        self.assertEqual(expired["status"], "TIMED_OUT")
        self.assertEqual(expired["timeout_phase"], "DISPATCH_STALE")

    def test_zero_dispatch_timeout_keeps_request_pending_until_claimed(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            created = cognition.submit(
                agent_id="ferro",
                purpose="durable-chatgpt",
                messages=[{"role": "user", "content": "hello"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=0,
                request_id="llmreq_durable_dispatch",
            )
        self.assertIsNone(created["dispatch_deadline_at"])
        self.assertIsNone(created["deadline_at"])

        with patch.object(cognition.time, "time", return_value=5000.0):
            pending = cognition.get("llmreq_durable_dispatch")
            claimed = cognition.claim_request(
                request_id="llmreq_durable_dispatch",
                watcher_id="watcher_late_session",
                claim_seconds=120,
            )
        self.assertEqual(pending["status"], "PENDING")
        self.assertEqual(claimed["status"], "DISPATCHED")
        self.assertEqual(claimed["response_deadline_at"], 5300.0)
        self.assertEqual(claimed["deadline_at"], 5300.0)

    def test_claim_extends_dispatch_grace_to_full_response_window(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="interactive-fallback",
                messages=[{"role": "user", "content": "hello"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=15,
                request_id="llmreq_claim_extends",
            )
        with patch.object(cognition.time, "time", return_value=1005.0):
            claimed = cognition.claim_request(
                request_id="llmreq_claim_extends",
                watcher_id="watcher_live",
                claim_seconds=120,
            )
        self.assertEqual(claimed["status"], "DISPATCHED")
        self.assertEqual(claimed["dispatch_deadline_at"], 1015.0)
        self.assertEqual(claimed["response_deadline_at"], 1305.0)
        self.assertEqual(claimed["deadline_at"], 1305.0)

        with patch.object(cognition.time, "time", return_value=1016.0):
            still_live = cognition.get("llmreq_claim_extends")
        self.assertEqual(still_live["status"], "DISPATCHED")

    def test_peek_keeps_expired_dispatch_grace_request_claimable(self) -> None:
        with patch.object(cognition.time, "time", return_value=1000.0):
            cognition.submit(
                agent_id="ferro",
                purpose="interactive-fallback",
                messages=[{"role": "user", "content": "hello"}],
                timeout_seconds=300,
                dispatch_timeout_seconds=5,
                request_id="llmreq_peek_expired",
            )
        with patch.object(cognition.time, "time", return_value=1006.0):
            pending = cognition.peek_next(agent_id="ferro")
        self.assertEqual(pending["request_id"], "llmreq_peek_expired")
        self.assertEqual(pending["status"], "PENDING")
        self.assertEqual(pending["dispatch_waiting_since"], 1005.0)
        stored = json.loads(
            (self.root / "requests" / "llmreq_peek_expired.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(stored["status"], "PENDING")
        self.assertEqual(stored["dispatch_waiting_since"], 1005.0)
        self.assertIsNone(stored["deadline_at"])

    def test_same_watcher_can_resume_its_active_claim_after_widget_reload(self) -> None:
        created = self.submit("llmreq_resume_same_watcher")
        claimed = cognition.claim_request(
            request_id=created["request_id"],
            watcher_id="watcher_session_a",
            claim_seconds=300,
        )
        same = cognition.peek_next(
            agent_id="ferro",
            watcher_id="watcher_session_a",
        )
        self.assertEqual(same["request_id"], created["request_id"])
        self.assertEqual(same["status"], "DISPATCHED")
        self.assertTrue(same["owned_by_watcher"])
        self.assertFalse(same["reclaimable"])

        other = cognition.peek_next(
            agent_id="ferro",
            watcher_id="watcher_session_b",
        )
        self.assertIsNone(other)

        resumed = cognition.wait_pending(
            agent_id="ferro",
            watcher_id="watcher_session_a",
            timeout_seconds=1,
        )
        self.assertFalse(resumed["timed_out"])
        self.assertEqual(resumed["request"]["request_id"], created["request_id"])

        same_claim = cognition.claim_request(
            request_id=created["request_id"],
            watcher_id="watcher_session_a",
            claim_seconds=300,
        )
        self.assertEqual(
            same_claim["claim"]["token"],
            claimed["claim"]["token"],
        )

    def test_reopening_watcher_reuses_active_claim_owner(self) -> None:
        created = self.submit("llmreq_reopen_owner")
        claimed = cognition.claim_request(
            request_id=created["request_id"],
            watcher_id="watcher_session_survives_reload",
            claim_seconds=300,
        )
        with patch.object(cognition.time, "time", return_value=claimed["updated_at"] + 5):
            watcher = cognition.resumable_watcher_id("ferro")
            resumed = cognition.peek_next(
                agent_id="ferro",
                watcher_id=watcher,
            )

        self.assertEqual(watcher, "watcher_session_survives_reload")
        self.assertEqual(resumed["request_id"], created["request_id"])
        self.assertTrue(resumed["owned_by_watcher"])

    def test_new_watcher_is_created_when_no_active_claim_exists(self) -> None:
        first = cognition.resumable_watcher_id("ferro")
        second = cognition.resumable_watcher_id("ferro")
        self.assertTrue(first.startswith("watcher_ferro_"))
        self.assertTrue(second.startswith("watcher_ferro_"))
        self.assertNotEqual(first, second)

    def test_agent_queues_are_isolated(self) -> None:
        self.submit("llmreq_ferro", agent_id="ferro")
        self.submit("llmreq_other", agent_id="other-agent")
        claimed = cognition.claim_next(agent_id="other-agent", watcher_id="watcher_other")
        self.assertEqual(claimed["request_id"], "llmreq_other")
        ferro = cognition.get("llmreq_ferro")
        self.assertEqual(ferro["status"], "PENDING")

    def test_pi_request_preserves_tool_schemas_and_completion_can_return_tool_calls(self) -> None:
        created = cognition.submit(
            agent_id="pi-remote",
            purpose="pi.agent.model-call",
            messages=[{"role": "user", "content": "Inspect README."}],
            tools=[
                {
                    "name": "read",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ],
            request_id="llmreq_pi_tool",
        )
        self.assertEqual(created["tools"][0]["name"], "read")
        claimed = cognition.claim_request(
            request_id=created["request_id"],
            watcher_id="watcher_pi",
            claim_seconds=30,
        )
        done = cognition.complete(
            request_id=created["request_id"],
            claim_token=claimed["claim"]["token"],
            tool_calls=[
                {
                    "id": "call_read_1",
                    "name": "read",
                    "arguments": {"path": "README.md"},
                }
            ],
        )
        self.assertEqual(done["status"], "COMPLETED")
        self.assertEqual(done["response"]["text"], "")
        self.assertEqual(done["response"]["tool_calls"][0]["name"], "read")
        self.assertEqual(done["response"]["tool_calls"][0]["arguments"]["path"], "README.md")

    def test_completion_rejects_empty_model_output(self) -> None:
        created = self.submit("llmreq_empty")
        claimed = cognition.claim_request(
            request_id=created["request_id"], watcher_id="watcher_empty", claim_seconds=30
        )
        with self.assertRaisesRegex(ValueError, "response must contain"):
            cognition.complete(
                request_id=created["request_id"],
                claim_token=claimed["claim"]["token"],
                response_text="",
                tool_calls=[],
            )

    def test_transport_cli_forwards_pi_tool_contract_and_waits_for_same_request(self) -> None:
        payload = {
            "agent_id": "pi-remote",
            "purpose": "pi.agent.model-call",
            "messages": [{"role": "user", "content": "Inspect README."}],
            "tools": [{"name": "read", "description": "Read", "parameters": {}}],
            "timeout_seconds": 45,
        }
        with patch.object(
            cognition_cli, "submit", return_value={"request_id": "llmreq_cli"}
        ) as submit_mock, patch.object(
            cognition_cli,
            "wait_response",
            return_value={"request_id": "llmreq_cli", "status": "COMPLETED"},
        ) as wait_mock:
            result = cognition_cli.submit_wait(payload)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(submit_mock.call_args.kwargs["tools"][0]["name"], "read")
        wait_mock.assert_called_once_with("llmreq_cli", timeout_seconds=45)

    def test_transport_cli_forwards_optional_dispatch_grace(self) -> None:
        payload = {
            "agent_id": "ferro",
            "purpose": "general_text",
            "messages": [{"role": "user", "content": "hello"}],
            "timeout_seconds": 300,
            "dispatch_timeout_seconds": 15,
        }
        with patch.object(
            cognition_cli, "submit", return_value={"request_id": "llmreq_cli_grace"}
        ) as submit_mock, patch.object(
            cognition_cli,
            "wait_response",
            return_value={"request_id": "llmreq_cli_grace", "status": "COMPLETED"},
        ):
            cognition_cli.submit_wait(payload)
        self.assertEqual(
            submit_mock.call_args.kwargs["dispatch_timeout_seconds"], 15
        )

    def test_compat_cognitionctl_preserves_structured_tool_calls(self) -> None:
        with patch.object(
            cognitionctl,
            "complete",
            return_value={"request_id": "llmreq_compat", "status": "COMPLETED"},
        ) as complete_mock:
            result = cognitionctl.complete_request(
                request_id="llmreq_compat",
                claim_token="token",
                body={"tool_calls": [{"id": "c1", "name": "read", "arguments": {}}]},
                provider="livingruntime-chatgpt",
                model="chatgpt-web",
                session_id="session",
            )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(
            complete_mock.call_args.kwargs["tool_calls"][0]["name"], "read"
        )

    def test_compat_cognitionctl_wait_resumes_existing_request_without_submit(self) -> None:
        completed = {
            "request_id": "llmreq_resume",
            "status": "COMPLETED",
            "response": {"text": "done"},
        }
        with patch.object(
            sys,
            "argv",
            [
                "cognitionctl.py",
                "wait",
                "llmreq_resume",
                "--wait-timeout-seconds",
                "5",
            ],
        ), patch.object(
            cognitionctl,
            "wait_response",
            return_value=completed,
        ) as wait_mock, patch.object(
            cognitionctl,
            "submit",
            side_effect=AssertionError("wait must not resubmit"),
        ), patch.object(
            cognitionctl,
            "_print",
        ) as print_mock:
            self.assertEqual(cognitionctl.main(), 0)

        wait_mock.assert_called_once_with("llmreq_resume", timeout_seconds=5)
        print_mock.assert_called_once_with(completed)


if __name__ == "__main__":
    unittest.main()
