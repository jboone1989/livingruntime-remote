from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LIVINGRUNTIME_RELAY_ISSUER", "https://auth.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_AUDIENCE", "https://remote.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_JWKS_URL", "https://auth.example.test/jwks.json")
os.environ.setdefault("LIVINGRUNTIME_RELAY_RESOURCE_URL", "https://remote.example.test/mcp")
os.environ.setdefault("OPENAI_APPS_CHALLENGE", "challenge-token")

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT.parent / "plugin" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from starlette.testclient import TestClient

import server
from store import RelayStore


def _paired(store: RelayStore):
    code = store.create_pairing_code("user-a")["code"]
    return store.pair_device(code, "owner-main")


def _last_seen(store: RelayStore, device_id: str) -> float:
    with store.db() as db:
        return float(
            db.execute(
                "SELECT last_seen FROM devices WHERE device_id=?",
                (device_id,),
            ).fetchone()["last_seen"]
        )


def test_authentication_can_skip_execution_heartbeat() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RelayStore(str(Path(tmp) / "relay.sqlite3"))
        paired = _paired(store)
        with store.db() as db:
            db.execute(
                "UPDATE devices SET last_seen=? WHERE device_id=?",
                (1000.0, paired["device_id"]),
            )

        store.authenticate_device(
            paired["device_token"],
            touch_last_seen=False,
        )
        assert _last_seen(store, paired["device_id"]) == 1000.0

        store.authenticate_device(paired["device_token"])
        assert _last_seen(store, paired["device_id"]) > 1000.0


def test_device_event_does_not_refresh_execution_liveness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "relay.sqlite3")
        store = RelayStore(db_path)
        paired = _paired(store)
        with store.db() as db:
            db.execute(
                "UPDATE devices SET last_seen=? WHERE device_id=?",
                (1000.0, paired["device_id"]),
            )

        app = server.create_app(store)
        headers = {"authorization": "Bearer " + paired["device_token"]}
        payload = {
            "eventId": "lrcomp_test_execution_liveness",
            "name": "job.completed",
            "timestamp": "2026-10-01T00:00:00Z",
            "data": {
                "event_id": "lrcomp_test_execution_liveness",
                "job_id": "lrjob_test_execution_liveness",
                "status": "SUCCEEDED",
                "project": "agent-runtime",
                "device": "main",
            },
            "cursor": None,
        }

        with TestClient(app) as client:
            response = client.post(
                "/device/event",
                json=payload,
                headers=headers,
            )

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert _last_seen(store, paired["device_id"]) == 1000.0
