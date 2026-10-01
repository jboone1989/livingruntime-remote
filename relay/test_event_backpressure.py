from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


def test_unsubscribed_device_event_applies_server_backpressure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RelayStore(str(Path(tmp) / "relay.sqlite3"))
        code = store.create_pairing_code("user-a")["code"]
        paired = store.pair_device(code, "owner-main")
        app = server.create_app(store)
        headers = {"authorization": "Bearer " + paired["device_token"]}
        payload = {
            "eventId": "lrcomp_no_subscriber",
            "name": "job.completed",
            "timestamp": "2026-10-01T00:00:00Z",
            "data": {
                "event_id": "lrcomp_no_subscriber",
                "job_id": "lrjob_no_subscriber",
                "status": "SUCCEEDED",
                "project": "agent-runtime",
                "device": "main",
            },
            "cursor": None,
        }
        sleep = AsyncMock()

        with patch.object(server.asyncio, "sleep", sleep), TestClient(app) as client:
            response = client.post("/device/event", json=payload, headers=headers)

        assert response.status_code == 200, response.text
        assert response.json()["matching_subscriptions"] == 0
        sleep.assert_awaited_once_with(1.0)
