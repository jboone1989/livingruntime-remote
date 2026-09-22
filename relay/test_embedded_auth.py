from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

os.environ.setdefault("LIVINGRUNTIME_RELAY_ISSUER", "https://auth.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_AUDIENCE", "https://remote.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_JWKS_URL", "https://auth.example.test/jwks.json")
os.environ.setdefault("LIVINGRUNTIME_RELAY_RESOURCE_URL", "https://remote.example.test/mcp")

from starlette.testclient import TestClient

import server
from embedded_auth import EmbeddedAuthStore
from store import RelayStore


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class EmbeddedOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.relay_db = str(root / "relay.sqlite3")
        self.auth_db = str(root / "auth.sqlite3")
        self.issuer = "https://remote.example.test"
        self.resource = self.issuer + "/mcp"
        self.email = "owner@example.test"
        self.password = "correct horse battery staple"
        EmbeddedAuthStore(self.auth_db).create_user(
            self.email, self.password, email_verified=True
        )
        self.env = patch.dict(
            os.environ,
            {
                "LIVINGRUNTIME_AUTH_MODE": "embedded",
                "LIVINGRUNTIME_RELAY_ISSUER": self.issuer,
                "LIVINGRUNTIME_RELAY_RESOURCE_URL": self.resource,
                "LIVINGRUNTIME_RELAY_DB": self.relay_db,
                "LIVINGRUNTIME_AUTH_DB": self.auth_db,
            },
        )
        self.env.start()
        self.store = RelayStore(self.relay_db)
        self.app = server.create_app(self.store)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def register(self, client: TestClient, scope: str = "remote:read remote:write openid email") -> dict:
        response = client.post(
            "/register",
            json={
                "client_name": "ChatGPT test",
                "redirect_uris": ["https://client.example.test/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": scope,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def authorize_and_login(
        self,
        client: TestClient,
        client_id: str,
        *,
        scope: str = "remote:read remote:write openid email",
    ) -> tuple[str, str]:
        verifier = "pkce-verifier-" + "x" * 48
        response = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://client.example.test/callback",
                "scope": scope,
                "state": "state-123",
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "resource": self.resource,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302, response.text)
        login = urlsplit(response.headers["location"])
        self.assertEqual(login.path, "/oauth/login")
        request_id = parse_qs(login.query)["request_id"][0]

        page = client.get(f"/oauth/login?request_id={request_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sign in to LivingRuntime Remote", page.text)

        denied = client.post(
            "/oauth/login",
            data={"request_id": request_id, "email": self.email, "password": "wrong-password"},
            follow_redirects=False,
        )
        self.assertEqual(denied.status_code, 401)

        logged_in = client.post(
            "/oauth/login",
            data={"request_id": request_id, "email": self.email, "password": self.password},
            follow_redirects=False,
        )
        self.assertEqual(logged_in.status_code, 303, logged_in.text)
        callback = urlsplit(logged_in.headers["location"])
        self.assertEqual(callback.scheme + "://" + callback.netloc + callback.path,
                         "https://client.example.test/callback")
        query = parse_qs(callback.query)
        self.assertEqual(query["state"], ["state-123"])
        return query["code"][0], verifier

    def exchange(self, client: TestClient, client_id: str, code: str, verifier: str) -> dict:
        response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": "https://client.example.test/callback",
                "code_verifier": verifier,
                "resource": self.resource,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_metadata_registration_pkce_refresh_revoke_and_mcp_access(self):
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            metadata = client.get("/.well-known/oauth-authorization-server")
            self.assertEqual(metadata.status_code, 200)
            meta = metadata.json()
            self.assertEqual(meta["issuer"], self.issuer)
            self.assertTrue(meta["registration_endpoint"].endswith("/register"))
            self.assertTrue(meta["revocation_endpoint"].endswith("/revoke"))
            self.assertEqual(meta["token_endpoint_auth_methods_supported"], ["none"])
            self.assertEqual(meta["revocation_endpoint_auth_methods_supported"], ["none"])
            self.assertEqual(
                set(meta["scopes_supported"]),
                {"remote:read", "remote:write", "openid", "email"},
            )
            self.assertTrue(meta["userinfo_endpoint"].endswith("/userinfo"))
            oidc = client.get("/.well-known/openid-configuration")
            self.assertEqual(oidc.status_code, 200)
            self.assertEqual(oidc.json()["issuer"], self.issuer)

            resource_meta = client.get("/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(resource_meta.status_code, 200)
            self.assertEqual(
                set(resource_meta.json()["scopes_supported"]),
                {"remote:read", "remote:write", "openid", "email"},
            )

            registration = self.register(client)
            client_id = registration["client_id"]
            self.assertEqual(registration["token_endpoint_auth_method"], "none")
            self.assertNotIn("client_secret", registration)

            code, verifier = self.authorize_and_login(client, client_id)
            tokens = self.exchange(client, client_id, code, verifier)
            access = tokens["access_token"]
            refresh = tokens["refresh_token"]
            self.assertEqual(
                set(tokens["scope"].split()),
                {"remote:read", "remote:write", "openid", "email"},
            )

            userinfo = client.get(
                "/userinfo", headers={"authorization": "Bearer " + access}
            )
            self.assertEqual(userinfo.status_code, 200, userinfo.text)
            self.assertEqual(userinfo.json()["email"], self.email)
            self.assertTrue(userinfo.json()["email_verified"])
            self.assertTrue(str(userinfo.json()["sub"]).startswith("usr_"))

            tools = client.post(
                "/mcp",
                headers={"authorization": "Bearer " + access},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            self.assertEqual(tools.status_code, 200, tools.text)
            names = {tool["name"] for tool in tools.json()["result"]["tools"]}
            self.assertIn("connection_status", names)
            self.assertIn("write_file", names)

            rotated = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh,
                    "scope": "remote:read remote:write",
                    "resource": self.resource,
                },
            )
            self.assertEqual(rotated.status_code, 200, rotated.text)
            rotated_tokens = rotated.json()
            self.assertNotEqual(rotated_tokens["refresh_token"], refresh)

            reused = client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh,
                },
            )
            self.assertEqual(reused.status_code, 400)
            self.assertEqual(reused.json()["error"], "invalid_grant")

            revoked_access = rotated_tokens["access_token"]
            revoked = client.post(
                "/revoke",
                data={"client_id": client_id, "client_secret": "", "token": revoked_access},
            )
            self.assertEqual(revoked.status_code, 200, revoked.text)
            blocked = client.post(
                "/mcp",
                headers={"authorization": "Bearer " + revoked_access},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            self.assertEqual(blocked.status_code, 401)

    def test_scope_validation_rejects_unknown_scope(self):
        with TestClient(self.app) as client:
            response = client.post(
                "/register",
                json={
                    "client_name": "bad scope",
                    "redirect_uris": ["https://client.example.test/callback"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": "remote:read remote:admin",
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "invalid_client_metadata")


if __name__ == "__main__":
    unittest.main()
