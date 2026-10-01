from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LIVINGRUNTIME_RELAY_ISSUER", "https://auth.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_AUDIENCE", "https://remote.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_JWKS_URL", "https://auth.example.test/jwks.json")
os.environ.setdefault("LIVINGRUNTIME_RELAY_RESOURCE_URL", "https://remote.example.test/mcp")
os.environ.setdefault(
    "LIVINGRUNTIME_BROWSER_TAKEOVER_PUBLIC_ORIGIN",
    "https://remote.example.test",
)
os.environ.setdefault("OPENAI_APPS_CHALLENGE", "challenge-token")

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT.parent / "plugin" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from starlette.testclient import TestClient

import server
from store import RelayStore, digest


def test_browser_takeover_token_is_hashed_and_expires() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RelayStore(str(Path(tmp) / "relay.sqlite3"))
        grant = store.create_browser_takeover_token(
            "user-a",
            "owner-main:chatgpt",
            "chatgpt",
            ttl_seconds=600,
        )

        with store.db() as db:
            row = db.execute(
                "SELECT token_hash,target_key,profile_id FROM browser_takeover_tokens"
            ).fetchone()

        assert row is not None
        assert row["token_hash"] == digest(grant["token"])
        assert row["token_hash"] != grant["token"]
        assert row["target_key"] == "owner-main:chatgpt"
        assert row["profile_id"] == "chatgpt"
        assert store.browser_takeover_token(grant["token"]) is not None

        with store.db() as db:
            db.execute(
                "UPDATE browser_takeover_tokens SET expires_at=0 WHERE token_hash=?",
                (digest(grant["token"]),),
            )
        assert store.browser_takeover_token(grant["token"]) is None


def test_takeover_target_allowlist_accepts_only_loopback_http() -> None:
    with patch.dict(
        os.environ,
        {
            "LIVINGRUNTIME_BROWSER_TAKEOVER_TARGETS": json.dumps(
                {"owner-main:chatgpt": "http://127.0.0.1:16080"}
            )
        },
        clear=False,
    ):
        assert (
            server._browser_takeover_target("owner-main:chatgpt")
            == "http://127.0.0.1:16080"
        )

    for invalid in (
        "https://127.0.0.1:16080",
        "http://0.0.0.0:16080",
        "http://example.com:16080",
        "http://127.0.0.1:16080/other",
    ):
        with patch.dict(
            os.environ,
            {
                "LIVINGRUNTIME_BROWSER_TAKEOVER_TARGETS": json.dumps(
                    {"owner-main:chatgpt": invalid}
                )
            },
            clear=False,
        ):
            try:
                server._browser_takeover_targets()
            except RuntimeError:
                pass
            else:
                raise AssertionError(f"unsafe takeover target accepted: {invalid}")


def test_browser_takeover_http_proxy_requires_valid_token() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ,
        {
            "LIVINGRUNTIME_BROWSER_TAKEOVER_TARGETS": json.dumps(
                {"owner-main:chatgpt": "http://127.0.0.1:16080"}
            )
        },
        clear=False,
    ):
        store = RelayStore(str(Path(tmp) / "relay.sqlite3"))
        grant = store.create_browser_takeover_token(
            "user-a",
            "owner-main:chatgpt",
            "chatgpt",
            ttl_seconds=600,
        )
        app = server.create_app(store)

        with patch.object(
            server,
            "_browser_takeover_http_get",
            return_value=(200, {"content-type": "text/html"}, b"<html>ok</html>"),
        ) as fetch, TestClient(app) as client:
            response = client.get(
                f"/browser-takeover/{grant['token']}/vnc.html?autoconnect=1"
            )
            missing = client.get("/browser-takeover/not-a-token/vnc.html")

        assert response.status_code == 200
        assert response.text == "<html>ok</html>"
        assert response.headers["cache-control"] == "no-store"
        fetch.assert_called_once_with(
            "http://127.0.0.1:16080",
            "vnc.html",
            "autoconnect=1",
        )
        assert missing.status_code == 404
