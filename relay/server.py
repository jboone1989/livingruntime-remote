from __future__ import annotations

import asyncio
import html
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

from auth import verifier_from_env
from embedded_auth import EmbeddedAuthStore, EmbeddedOAuthProvider
from store import RelayStore

NAME = "LivingRuntime Remote"
VERSION = "0.4.4"
READ = {"securitySchemes": [{"type": "oauth2", "scopes": ["remote:read"]}]}
WRITE = {"securitySchemes": [{"type": "oauth2", "scopes": ["remote:read", "remote:write"]}]}
SUPPORTED_SCOPES = ["remote:read", "remote:write"]


class PairRateLimiter:
    def __init__(self, limit: int = 10, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.attempts: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        recent = [value for value in self.attempts.get(key, []) if value >= cutoff]
        if len(recent) >= self.limit:
            self.attempts[key] = recent
            return False
        recent.append(now)
        self.attempts[key] = recent
        return True


class Relay:
    def __init__(
        self,
        store: RelayStore,
        timeout: float = 45.0,
        reconnect_grace: float = 8.0,
        device_stale_after: float = 35.0,
    ) -> None:
        self.store = store
        self.timeout = timeout
        self.reconnect_grace = max(0.0, reconnect_grace)
        self.device_stale_after = max(1.0, device_stale_after)

    def device_status(self, user_sub: str) -> dict[str, Any] | None:
        device = self.store.device_for_user(user_sub)
        if not device:
            return None
        age = max(0.0, time.time() - float(device.get("last_seen") or 0.0))
        return {
            **device,
            "online": age <= self.device_stale_after,
            "stale_for_seconds": round(age, 3),
        }

    async def _wait_for_online_device(self, user_sub: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.reconnect_grace
        while True:
            device = self.device_status(user_sub)
            if device and device["online"]:
                return device
            if time.monotonic() >= deadline:
                if not device:
                    raise RuntimeError(
                        "no paired LivingRuntime Remote device; call create_pairing_code first"
                    )
                raise RuntimeError(
                    "paired LivingRuntime Remote device is offline "
                    f"(last seen {device['stale_for_seconds']}s ago)"
                )
            await asyncio.sleep(0.25)

    async def call(self, user_sub: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        device = await self._wait_for_online_device(user_sub)
        task_id = self.store.enqueue(user_sub, device["device_id"], tool, args)
        requested_timeout = 0.0
        try:
            requested_timeout = float(args.get("timeout_seconds") or 0.0)
        except (TypeError, ValueError):
            requested_timeout = 0.0
        wait_timeout = max(self.timeout, min(120.0, max(0.0, requested_timeout)) + 30.0)
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            result = self.store.result(user_sub, task_id, consume=True)
            if result is not None:
                if not result.get("ok"):
                    raise RuntimeError(str(result.get("error") or "remote device call failed"))
                value = result.get("result")
                return value if isinstance(value, dict) else {"result": value}
            await asyncio.sleep(0.15)
        if self.store.cancel_if_queued(user_sub, task_id):
            raise TimeoutError(
                "paired device did not claim task before relay timeout; queued task was cancelled"
            )
        raise TimeoutError(
            "paired device claimed task but did not complete before relay timeout"
        )


def _principal(scope: str) -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise PermissionError("OAuth authentication required")
    if scope not in token.scopes:
        raise PermissionError(f"OAuth scope required: {scope}")
    return token.subject


def _auth_mode() -> str:
    mode = os.environ.get("LIVINGRUNTIME_AUTH_MODE", "external").strip().lower()
    if mode not in {"external", "embedded"}:
        raise RuntimeError("LIVINGRUNTIME_AUTH_MODE must be external or embedded")
    return mode


def _append_query(url: str, values: dict[str, str | None]) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((key, value) for key, value in values.items() if value is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _login_page(request_id: str, *, error: str | None = None, email: str = "") -> HTMLResponse:
    error_html = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LivingRuntime Remote sign in</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#111;color:#eee;margin:0}}
main{{max-width:420px;margin:10vh auto;padding:28px}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:24px}}
h1{{font-size:22px;margin:0 0 8px}}
p{{color:#aaa;line-height:1.5}}
label{{display:block;margin:16px 0 6px;font-size:14px}}
input{{box-sizing:border-box;width:100%;padding:12px;border-radius:10px;border:1px solid #444;background:#0d0d0d;color:#fff}}
button{{width:100%;margin-top:20px;padding:12px;border:0;border-radius:10px;font-weight:700;cursor:pointer}}
.error{{color:#ff9d9d}}
.small{{font-size:12px}}
</style>
</head>
<body><main><div class="card">
<h1>Sign in to LivingRuntime Remote</h1>
<p>Authorize ChatGPT to access your paired LivingRuntime Remote device.</p>
{error_html}
<form method="post" action="/oauth/login">
<input type="hidden" name="request_id" value="{html.escape(request_id, quote=True)}">
<label for="email">Email</label>
<input id="email" name="email" type="email" autocomplete="username" required value="{html.escape(email, quote=True)}">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Continue</button>
</form>
<p class="small">Credentials are used only by this LivingRuntime Remote authorization server.</p>
</div></main></body></html>"""
    return HTMLResponse(
        body,
        headers={
            "cache-control": "no-store",
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            "x-content-type-options": "nosniff",
        },
    )


def create_mcp(
    relay: Relay,
    embedded_provider: EmbeddedOAuthProvider | None = None,
) -> MCPServer:
    issuer = os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/")
    resource = os.environ["LIVINGRUNTIME_RELAY_RESOURCE_URL"]
    docs_url = os.environ.get("LIVINGRUNTIME_RELAY_DOCS_URL")
    mode = _auth_mode()

    auth_settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource,
        required_scopes=["remote:read"],
        validate_token_resource=True,
        service_documentation_url=docs_url,
        client_registration_options=(
            ClientRegistrationOptions(
                enabled=True,
                valid_scopes=SUPPORTED_SCOPES,
                default_scopes=["remote:read"],
            )
            if mode == "embedded"
            else None
        ),
        revocation_options=RevocationOptions(enabled=mode == "embedded"),
    )
    kwargs: dict[str, Any]
    if mode == "embedded":
        if embedded_provider is None:
            auth_db = os.environ.get("LIVINGRUNTIME_AUTH_DB", relay.store.path)
            embedded_provider = EmbeddedOAuthProvider(EmbeddedAuthStore(auth_db), issuer)
        kwargs = {"auth_server_provider": embedded_provider}
    else:
        kwargs = {"token_verifier": verifier_from_env()}

    server = MCPServer(
        NAME,
        version=VERSION,
        auth=auth_settings,
        **kwargs,
    )

    if embedded_provider is not None:
        @server.custom_route("/oauth/login", methods=["GET", "POST"], include_in_schema=False)
        async def oauth_login(request: Request):
            request_id = request.query_params.get("request_id", "")
            if request.method == "POST":
                raw = await request.body()
                if len(raw) > 8192:
                    return PlainTextResponse("request too large", status_code=413)
                form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
                request_id = (form.get("request_id") or [""])[0]
                email = (form.get("email") or [""])[0].strip()
                password = (form.get("password") or [""])[0]
                pending = embedded_provider.store.load_pending_auth(request_id)
                if not pending:
                    return PlainTextResponse("authorization request expired", status_code=400)
                subject = embedded_provider.store.authenticate_user(email, password)
                if subject is None:
                    response = _login_page(
                        request_id,
                        error="Invalid email or password.",
                        email=email,
                    )
                    response.status_code = 401
                    return response
                code, state = embedded_provider.store.consume_pending_auth(request_id, subject)
                return RedirectResponse(
                    _append_query(
                        str(code.redirect_uri),
                        {"code": code.code, "state": state},
                    ),
                    status_code=303,
                    headers={"cache-control": "no-store"},
                )

            if not request_id or embedded_provider.store.load_pending_auth(request_id) is None:
                return PlainTextResponse("authorization request expired", status_code=400)
            return _login_page(request_id)

    @server.tool(name="create_pairing_code", annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=False), meta=WRITE)
    def create_pairing_code() -> dict[str, Any]:
        """Create a short-lived one-time code that pairs the user's local connector."""
        return relay.store.create_pairing_code(_principal("remote:write"))

    @server.tool(name="device_status", annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False), meta=READ)
    def device_status() -> dict[str, Any]:
        """Report paired-device recency and online state without exposing credentials."""
        user = _principal("remote:read")
        device = relay.device_status(user)
        return {"paired": bool(device), "device": device}

    @server.tool(name="disconnect_device", annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, openWorldHint=False), meta=WRITE)
    def disconnect_device(device_id: str | None = None) -> dict[str, Any]:
        """Revoke a paired connector and discard its queued or completed relay tasks."""
        return {"disconnected": relay.store.revoke_device(_principal("remote:write"), device_id)}

    def expose(name: str, read_only: bool, open_world: bool, destructive: bool):
        meta = READ if read_only else WRITE
        annotations = ToolAnnotations(
            readOnlyHint=read_only,
            openWorldHint=open_world,
            destructiveHint=destructive,
            idempotentHint=False if destructive else None,
        )
        return server.tool(name=name, annotations=annotations, meta=meta)

    @expose("connection_status", True, False, False)
    async def connection_status() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "connection_status", {})

    @expose("capabilities", True, False, False)
    async def capabilities() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "capabilities", {})

    @expose("list_projects", True, False, False)
    async def list_projects() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "list_projects", {})

    @expose("read_file", True, False, False)
    async def read_file(path: str, project: str | None = None, offset: int = 0,
                        max_bytes: int = 131072) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "read_file", locals())

    @expose("list_dir", True, False, False)
    async def list_dir(path: str | None = None, project: str | None = None,
                       max_entries: int = 200) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "list_dir", locals())

    @expose("logs", True, False, False)
    async def logs(unit: str | None = None, project: str | None = None, lines: int = 200,
                   since_minutes: int = 60) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "logs", locals())

    @expose("write_file", False, False, True)
    async def write_file(path: str, content: str, project: str | None = None,
                         mode: str = "replace", expected_sha256: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "write_file", locals())

    @expose("git", False, True, True)
    async def git(args: list[str], repo_path: str | None = None, project: str | None = None,
                  timeout_seconds: int = 30) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "git", locals())

    @expose("exec", False, True, True)
    async def exec(argv: list[str], cwd: str | None = None, project: str | None = None,
                   timeout_seconds: int = 30) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "exec", locals())

    @expose("process", False, False, True)
    async def process(action: str, pid: int | None = None, contains: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "process", locals())

    @expose("systemd", False, False, True)
    async def systemd(action: str, unit: str | None = None, project: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "systemd", locals())

    @expose("apply_patch", False, False, True)
    async def apply_patch(path: str, patch: str, expected_sha256: str | None = None,
                          project: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "apply_patch", locals())

    @expose("diagnostics", True, False, False)
    async def diagnostics() -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "diagnostics", {})

    return server


def _device_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise PermissionError("device bearer token required")
    return value[7:]


def create_app(store: RelayStore | None = None) -> Starlette:
    store = store or RelayStore(os.environ.get("LIVINGRUNTIME_RELAY_DB", "state/relay.sqlite3"))
    relay = Relay(
        store,
        float(os.environ.get("LIVINGRUNTIME_RELAY_TIMEOUT", "45")),
        float(os.environ.get("LIVINGRUNTIME_RELAY_RECONNECT_GRACE", "8")),
        float(os.environ.get("LIVINGRUNTIME_RELAY_DEVICE_STALE_AFTER", "35")),
    )
    pair_limiter = PairRateLimiter(
        int(os.environ.get("LIVINGRUNTIME_RELAY_PAIR_LIMIT", "10")),
        float(os.environ.get("LIVINGRUNTIME_RELAY_PAIR_WINDOW", "60")),
    )

    embedded_provider: EmbeddedOAuthProvider | None = None
    if _auth_mode() == "embedded":
        auth_db = os.environ.get("LIVINGRUNTIME_AUTH_DB", store.path)
        auth_store = EmbeddedAuthStore(auth_db)
        bootstrap_email = os.environ.get("LIVINGRUNTIME_AUTH_BOOTSTRAP_EMAIL", "").strip()
        bootstrap_password = os.environ.get("LIVINGRUNTIME_AUTH_BOOTSTRAP_PASSWORD", "")
        if bootstrap_email and bootstrap_password:
            try:
                auth_store.create_user(bootstrap_email, bootstrap_password, email_verified=True)
            except ValueError as exc:
                if str(exc) != "account already exists":
                    raise
        embedded_provider = EmbeddedOAuthProvider(
            auth_store,
            os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/"),
        )

    issuer_parts = urlsplit(os.environ["LIVINGRUNTIME_RELAY_ISSUER"])
    public_host = issuer_parts.hostname or "127.0.0.1"
    public_origin = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            public_host,
            f"{public_host}:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ],
        allowed_origins=[
            public_origin,
            f"{issuer_parts.scheme}://{public_host}:*",
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )
    mcp_app = create_mcp(relay, embedded_provider).streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=public_host,
        transport_security=transport_security,
    )
    install_script_base = os.environ.get(
        "LIVINGRUNTIME_INSTALL_SCRIPT_BASE",
        "https://raw.githubusercontent.com/jboone1989/livingruntime-remote/main/"
        "plugin/scripts",
    ).rstrip("/")
    release_base = os.environ.get(
        "LIVINGRUNTIME_CONNECTOR_RELEASE_BASE",
        "https://github.com/jboone1989/livingruntime-remote/releases/latest/download",
    ).rstrip("/")

    async def health(_: Request):
        return JSONResponse({
            "ok": True,
            "service": NAME,
            "version": VERSION,
            "auth_mode": _auth_mode(),
        })

    async def challenge(_: Request):
        return PlainTextResponse(os.environ.get("OPENAI_APPS_CHALLENGE", ""))

    async def install_page(_: Request):
        origin = html.escape(public_origin)
        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Install LivingRuntime Remote</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#111;color:#eee;margin:0}}
main{{max-width:760px;margin:7vh auto;padding:28px}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:24px;margin:18px 0}}
code,pre{{background:#0d0d0d;border-radius:8px;padding:10px;overflow:auto}}
pre{{white-space:pre-wrap}}
p,li{{color:#bbb;line-height:1.55}}
a{{color:#b9ccff}}
</style>
</head>
<body><main>
<h1>LivingRuntime Remote Connector</h1>
<p>Install the small connector on a computer that already has key-based SSH access to your Linux host. Python is not required.</p>
<div class="card">
<h2>Windows</h2>
<pre>irm {origin}/install.ps1 | iex</pre>
</div>
<div class="card">
<h2>Linux / macOS</h2>
<pre>curl -fsSL {origin}/install.sh | sh</pre>
</div>
<p>The installer asks for the pairing code shown in ChatGPT, your SSH host, and the workspace root ChatGPT may access. It verifies SSH before consuming the pairing code.</p>
</main></body></html>"""
        return HTMLResponse(
            body,
            headers={
                "cache-control": "no-store",
                "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
                "x-content-type-options": "nosniff",
            },
        )

    async def install_ps1(_: Request):
        return RedirectResponse(f"{install_script_base}/install-connector.ps1", status_code=302)

    async def install_sh(_: Request):
        return RedirectResponse(f"{install_script_base}/install-connector.sh", status_code=302)

    async def download_connector(request: Request):
        assets = {
            "windows-amd64": "livingruntime-remote-connector-windows-amd64.exe",
            "linux-amd64": "livingruntime-remote-connector-linux-amd64",
            "linux-arm64": "livingruntime-remote-connector-linux-arm64",
            "macos-amd64": "livingruntime-remote-connector-macos-amd64",
            "macos-arm64": "livingruntime-remote-connector-macos-arm64",
        }
        asset = assets.get(request.path_params["target"])
        if asset is None:
            return PlainTextResponse("unsupported connector target", status_code=404)
        return RedirectResponse(f"{release_base}/{asset}", status_code=302)

    async def pair(request: Request):
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        client_key = forwarded or (request.client.host if request.client else "unknown")
        if not pair_limiter.allow(client_key):
            return JSONResponse(
                {"error": "too many pairing attempts"},
                status_code=429,
                headers={"retry-after": str(int(pair_limiter.window_seconds))},
            )
        try:
            body = await request.json()
            result = store.pair_device(str(body.get("code", "")), str(body.get("name", "")))
            return JSONResponse(result)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)

    async def poll(request: Request):
        try:
            device = store.authenticate_device(_device_token(request))
            wait = min(25.0, max(0.0, float(request.query_params.get("wait", "20"))))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                task = store.claim(device["device_id"])
                if task:
                    return JSONResponse({"task": task})
                await asyncio.sleep(0.25)
            return JSONResponse({"task": None})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)

    async def complete(request: Request):
        try:
            device = store.authenticate_device(_device_token(request))
            body = await request.json()
            store.complete(device["device_id"], str(body["task_id"]), dict(body["result"]))
            return JSONResponse({"ok": True})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def embedded_oauth_metadata(_: Request):
        issuer = os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/")
        payload = {
            "issuer": issuer,
            "authorization_endpoint": issuer + "/authorize",
            "token_endpoint": issuer + "/token",
            "registration_endpoint": issuer + "/register",
            "revocation_endpoint": issuer + "/revoke",
            "scopes_supported": SUPPORTED_SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }
        docs = os.environ.get("LIVINGRUNTIME_RELAY_DOCS_URL")
        if docs:
            payload["service_documentation"] = docs
        return JSONResponse(payload, headers={"cache-control": "no-store"})

    async def embedded_resource_metadata(_: Request):
        return JSONResponse({
            "resource": os.environ["LIVINGRUNTIME_RELAY_RESOURCE_URL"],
            "authorization_servers": [os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/")],
            "scopes_supported": SUPPORTED_SCOPES,
            "bearer_methods_supported": ["header"],
        }, headers={"cache-control": "no-store"})

    routes = [
        Route("/healthz", health),
        Route("/install", install_page),
        Route("/install.ps1", install_ps1),
        Route("/install.sh", install_sh),
        Route("/download/{target}", download_connector),
        Route("/.well-known/openai-apps-challenge", challenge),
    ]
    if _auth_mode() == "embedded":
        resource_path = urlsplit(os.environ["LIVINGRUNTIME_RELAY_RESOURCE_URL"]).path or ""
        routes.extend([
            Route("/.well-known/oauth-authorization-server", embedded_oauth_metadata),
            Route("/.well-known/oauth-protected-resource" + resource_path, embedded_resource_metadata),
        ])
    routes.extend([
        Route("/device/pair", pair, methods=["POST"]),
        Route("/device/poll", poll, methods=["POST"]),
        Route("/device/result", complete, methods=["POST"]),
        Mount("/", app=mcp_app),
    ])
    return Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("LIVINGRUNTIME_RELAY_BIND", "127.0.0.1"),
                port=int(os.environ.get("LIVINGRUNTIME_RELAY_PORT", "8770")))
