from __future__ import annotations

import asyncio
import html
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.apps import Apps
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

from auth import verifier_from_env
from embedded_auth import EmbeddedAuthStore, EmbeddedOAuthProvider
from store import RelayStore

NAME = "LivingRuntime Remote"
VERSION = "0.4.17"
PI_JOB_WIDGET_URI = "ui://livingruntime-remote/pi-job-watch-v1.html"
IDENTITY_SCOPES = ["openid", "email"]
SESSION_SCOPES = ["offline_access"]
READ = {"securitySchemes": [{"type": "oauth2", "scopes": ["remote:read", *IDENTITY_SCOPES]}]}
WRITE = {"securitySchemes": [{"type": "oauth2", "scopes": ["remote:read", "remote:write", *IDENTITY_SCOPES]}]}
SUPPORTED_SCOPES = ["remote:read", "remote:write", *IDENTITY_SCOPES, *SESSION_SCOPES]
TOOL_TEXT = {
    "create_pairing_code": ("Create pairing code", "Create a short-lived one-time code used to pair the user's local LivingRuntime Remote Connector."),
    "device_status": ("Check paired device", "Check whether the user's paired LivingRuntime Remote Connector is present and recently online."),
    "disconnect_device": ("Disconnect paired device", "Revoke a paired LivingRuntime Remote Connector and discard its queued or completed relay tasks."),
    "connection_status": ("Check connection status", "Check SSH, gateway, authentication, configured roots, projects, and paired-device reachability before remote work."),
    "capabilities": ("List Remote capabilities", "List the bounded Remote tools currently available and report whether the toolset is healthy and complete."),
    "list_projects": ("List configured projects", "List the named projects and bounded workspaces configured for this LivingRuntime Remote Connector."),
    "read_file": ("Read remote file", "Read bytes from a file inside an allowed project or configured root. Use this before editing or inspecting source files."),
    "list_dir": ("List remote directory", "List files and directories inside an allowed project or configured root without modifying them."),
    "logs": ("Read service logs", "Read recent journal logs for an allowlisted service, optionally scoped through a configured project."),
    "write_file": ("Write remote file", "Create or replace a file inside an allowed project or configured root, optionally guarded by an expected SHA-256."),
    "git": ("Run Git command", "Run a bounded Git command inside an allowed repository using explicit arguments."),
    "exec": ("Execute bounded command", "Run an allowlisted executable with explicit argv inside an allowed workspace. Shell command strings and credential overrides are blocked."),
    "process": ("Manage remote process", "Perform a permitted process action on the connected host using a PID or bounded process-name match."),
    "systemd": ("Manage allowlisted service", "Inspect or control an explicitly allowlisted systemd unit on the connected host."),
    "apply_patch": ("Apply file patch", "Apply a unified diff to a file inside an allowed workspace, optionally guarded by an expected SHA-256."),
    "diagnostics": ("Run Remote diagnostics", "Collect bounded LivingRuntime Remote diagnostics for connectivity, configuration, and tool-health troubleshooting."),
}

PI_JOB_WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0;
    padding: 12px;
    font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: var(--color-text-primary, inherit);
    background: transparent;
  }
  .card {
    border: 1px solid var(--color-border-secondary, rgba(127,127,127,.35));
    border-radius: 12px;
    padding: 12px 14px;
  }
  .row { display: flex; gap: 8px; align-items: center; }
  .dot {
    width: 8px; height: 8px; border-radius: 999px;
    background: var(--color-text-info, #4f7cff);
    flex: 0 0 auto;
  }
  #detail { margin-top: 6px; color: var(--color-text-secondary, #777); }
  code { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; }
</style>
</head>
<body>
  <div class="card">
    <div class="row"><span class="dot"></span><strong id="status">Preparing Pi job watcher…</strong></div>
    <div id="detail"></div>
  </div>
<script>
(() => {
  const pending = new Map();
  let nextId = 1;
  let connected = false;
  let latestOutput = null;
  let activeJob = null;
  let stopped = false;
  const statusEl = document.getElementById("status");
  const detailEl = document.getElementById("detail");

  function request(method, params) {
    const id = nextId++;
    window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }

  function notify(method, params = {}) {
    window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");
  }

  function setStatus(status, detail = "") {
    statusEl.textContent = status;
    detailEl.textContent = detail;
  }

  function toolResultData(result) {
    return result?.structuredContent || result?.structured_content || result || null;
  }

  async function sendFollowUp(jobId, state) {
    const completionStatus = state?.status || "UNKNOWN";
    const prompt =
      "Pi Remote job " + jobId + " completed with status " + completionStatus +
      ". Continue this same development task now. Inspect the durable Pi job/session result, " +
      "review the changes and tests, and proceed to the next required step without asking me to say continue.";
    try {
      await request("ui/message", {
        role: "user",
        content: [{ type: "text", text: prompt }]
      });
      return;
    } catch (error) {
      const openai = typeof window !== "undefined" ? window.openai : undefined;
      if (openai?.sendFollowUpMessage) {
        await openai.sendFollowUpMessage({ prompt, scrollToBottom: false });
        return;
      }
      throw error;
    }
  }

  async function watch(output) {
    const jobId = output?.jobId;
    const piRemoteDir = output?.piRemoteDir;
    const jobRoot = output?.jobRoot || null;
    if (!connected || !jobId || activeJob === jobId || stopped) return;
    activeJob = jobId;
    setStatus("Pi is working…", "Job " + jobId);

    try {
      while (!stopped) {
        const result = await request("tools/call", {
          name: "wait_pi_job_completion",
          arguments: {
            job_id: jobId,
            pi_remote_dir: piRemoteDir,
            job_root: jobRoot,
            timeout_seconds: 90
          }
        });
        const data = toolResultData(result);
        if (data?.terminal) {
          const state = data.state || {};
          setStatus(
            "Pi finished: " + (state.status || "terminal"),
            "Sending a follow-up into this ChatGPT conversation…"
          );
          await request("ui/update-model-context", {
            structuredContent: {
              piRemoteCompletion: {
                jobId,
                status: state.status || null,
                finishedAt: state.finishedAt || null
              }
            }
          }).catch(() => {});
          await sendFollowUp(jobId, state);
          setStatus("Follow-up sent", "ChatGPT can continue from the completed Pi job.");
          stopped = true;
          return;
        }
        if (!data?.timedOut) {
          throw new Error("Pi job wait returned without terminal state or timeout");
        }
        setStatus("Pi is still working…", "No ChatGPT status polling; watcher remains attached.");
      }
    } catch (error) {
      setStatus("Watcher stopped", String(error?.message || error));
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id !== undefined && pending.has(message.id)) {
      const waiter = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) waiter.reject(message.error);
      else waiter.resolve(message.result);
      return;
    }
    if (message.method === "ui/notifications/tool-result") {
      latestOutput = message.params?.structuredContent || null;
      void watch(latestOutput);
    }
    if (message.method === "ui/notifications/request-teardown") {
      stopped = true;
    }
  }, { passive: true });

  async function connect() {
    try {
      await request("ui/initialize", {
        appInfo: { name: "livingruntime-remote-pi-job-watch", version: "1.0.0" },
        appCapabilities: {},
        protocolVersion: "2026-01-26"
      });
      notify("ui/notifications/initialized");
      connected = true;
      if (!latestOutput && window.openai?.toolOutput) {
        latestOutput = window.openai.toolOutput;
      }
      await watch(latestOutput);
    } catch (error) {
      setStatus("Widget initialization failed", String(error?.message || error));
    }
  }

  void connect();
})();
</script>
</body>
</html>"""


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
        self._task_events: dict[str, asyncio.Event] = {}
        self._result_events: dict[str, asyncio.Event] = {}

    def _task_event(self, device_id: str) -> asyncio.Event:
        event = self._task_events.get(device_id)
        if event is None:
            event = asyncio.Event()
            self._task_events[device_id] = event
        return event

    def notify_task(self, device_id: str) -> None:
        """Wake a connector poll that is waiting for work on this relay process."""
        self._task_event(device_id).set()

    def arm_task_wait(self, device_id: str) -> None:
        """Clear any stale wakeup before checking the durable queue once."""
        self._task_event(device_id).clear()

    async def wait_for_task(self, device_id: str, timeout: float) -> bool:
        """Sleep without touching SQLite until work arrives or the long poll expires."""
        if timeout <= 0:
            return False
        event = self._task_event(device_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def _result_event(self, task_id: str) -> asyncio.Event:
        event = self._result_events.get(task_id)
        if event is None:
            event = asyncio.Event()
            self._result_events[task_id] = event
        return event

    def notify_result(self, task_id: str) -> None:
        """Wake an MCP request that is waiting for this task result."""
        event = self._result_events.get(task_id)
        if event is not None:
            event.set()

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
        result_event = self._result_event(task_id)
        self.notify_task(device["device_id"])
        requested_timeout = 0.0
        try:
            requested_timeout = float(args.get("timeout_seconds") or 0.0)
        except (TypeError, ValueError):
            requested_timeout = 0.0
        wait_timeout = max(self.timeout, min(120.0, max(0.0, requested_timeout)) + 30.0)
        try:
            result = self.store.result(user_sub, task_id, consume=True)
            if result is None:
                try:
                    await asyncio.wait_for(result_event.wait(), timeout=wait_timeout)
                except TimeoutError:
                    pass
                result = self.store.result(user_sub, task_id, consume=True)
            if result is not None:
                if not result.get("ok"):
                    raise RuntimeError(str(result.get("error") or "remote device call failed"))
                value = result.get("result")
                return value if isinstance(value, dict) else {"result": value}
            if self.store.cancel_if_queued(user_sub, task_id):
                raise TimeoutError(
                    "paired device did not claim task before relay timeout; queued task was cancelled"
                )
            raise TimeoutError(
                "paired device claimed task but did not complete before relay timeout"
            )
        finally:
            self._result_events.pop(task_id, None)


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
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
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
                default_scopes=SUPPORTED_SCOPES,
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

    async def pi_job_command(
        user_sub: str,
        command: str,
        job_id: str,
        pi_remote_dir: str,
        job_root: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if command not in {"job-status", "job-wait"}:
            raise ValueError("unsupported Pi job command")
        job_id = str(job_id).strip()
        pi_remote_dir = str(pi_remote_dir).strip()
        if not job_id or len(job_id) > 128:
            raise ValueError("job_id must be a bounded non-empty string")
        if not pi_remote_dir or len(pi_remote_dir) > 4096:
            raise ValueError("pi_remote_dir must be a bounded non-empty path")
        payload: dict[str, Any] = {"jobId": job_id}
        if job_root:
            if len(job_root) > 4096:
                raise ValueError("job_root path is too long")
            payload["jobRoot"] = job_root
        bounded_timeout = min(90, max(1, int(timeout_seconds)))
        if command == "job-wait":
            payload["timeoutMs"] = bounded_timeout * 1000
        result = await relay.call(
            user_sub,
            "exec",
            {
                "argv": [
                    "node",
                    str(Path(pi_remote_dir) / "cli.js"),
                    command,
                    json.dumps(payload, separators=(",", ":")),
                ],
                "cwd": pi_remote_dir,
                "project": None,
                "timeout_seconds": min(120, bounded_timeout + 10),
            },
        )
        if int(result.get("returncode", 1)) != 0:
            raise RuntimeError(str(result.get("stderr") or "Pi Remote job command failed"))
        stdout = result.get("stdout")
        if not isinstance(stdout, str) or not stdout.strip():
            raise RuntimeError("Pi Remote job command returned no JSON")
        parsed = json.loads(stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError("Pi Remote job command returned non-object JSON")
        return parsed

    async def wait_pi_job_until_terminal(
        user_sub: str,
        binding: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Wait in bounded event-driven chunks without creating model-side polling turns."""
        deadline = time.monotonic() + min(540, max(1, int(timeout_seconds)))
        latest: dict[str, Any] | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest or {"terminal": False, "timedOut": True, "state": None}
            latest = await pi_job_command(
                user_sub,
                "job-wait",
                str(binding["job_id"]),
                str(binding["pi_remote_dir"]),
                binding.get("job_root"),
                timeout_seconds=min(90, max(1, int(remaining))),
            )
            state = latest.get("state") if isinstance(latest.get("state"), dict) else {}
            if latest.get("terminal") or state.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return latest
            if not latest.get("timedOut"):
                return latest

    apps = Apps()
    apps.add_html_resource(
        PI_JOB_WIDGET_URI,
        PI_JOB_WIDGET_HTML,
        name="pi-job-watch",
        title="Pi Remote job watcher",
        description="Wait for an existing Pi Remote detached job and continue this conversation when it finishes.",
        prefers_border=True,
    )

    @apps.tool(
        resource_uri=PI_JOB_WIDGET_URI,
        visibility=["model", "app"],
        name="watch_pi_job",
        title="Watch Pi Remote job",
        description=(
            "Attach a no-polling watcher to an existing Pi Remote detached job. "
            "Use this after starting a Pi Remote job. The widget waits for terminal state "
            "and sends a follow-up message into this same conversation so ChatGPT can continue."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def watch_pi_job(
        job_id: str,
        pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
        job_root: str | None = None,
    ) -> dict[str, Any]:
        user_sub = _principal("remote:read")
        state = await pi_job_command(
            user_sub,
            "job-status",
            job_id,
            pi_remote_dir,
            job_root,
            timeout_seconds=15,
        )
        return {
            "jobId": job_id,
            "piRemoteDir": pi_remote_dir,
            "jobRoot": job_root,
            "state": state,
            "terminal": state.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"},
        }

    server = MCPServer(
        NAME,
        version=VERSION,
        auth=auth_settings,
        extensions=[apps],
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

    @server.tool(name="create_pairing_code", title=TOOL_TEXT["create_pairing_code"][0],
        description=TOOL_TEXT["create_pairing_code"][1], annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, openWorldHint=False), meta=WRITE)
    def create_pairing_code() -> dict[str, Any]:
        """Create a short-lived one-time code that pairs the user's local connector."""
        return relay.store.create_pairing_code(_principal("remote:write"))

    @server.tool(name="device_status", title=TOOL_TEXT["device_status"][0],
        description=TOOL_TEXT["device_status"][1], annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False), meta=READ)
    def device_status() -> dict[str, Any]:
        """Report paired-device recency and online state without exposing credentials."""
        user = _principal("remote:read")
        device = relay.device_status(user)
        if not device:
            return {"paired": False, "device": None}
        public_device = {
            "name": device.get("name"),
            "last_seen": device.get("last_seen"),
            "online": device.get("online"),
            "stale_for_seconds": device.get("stale_for_seconds"),
        }
        return {"paired": True, "device": public_device}

    @server.tool(name="disconnect_device", title=TOOL_TEXT["disconnect_device"][0],
        description=TOOL_TEXT["disconnect_device"][1], annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, openWorldHint=False), meta=WRITE)
    def disconnect_device(device_id: str | None = None) -> dict[str, Any]:
        """Revoke a paired connector and discard its queued or completed relay tasks."""
        return {"disconnected": relay.store.revoke_device(_principal("remote:write"), device_id)}

    @server.tool(
        name="wait_pi_job_completion",
        title="Wait for Pi Remote job completion",
        description=(
            "App-only long wait for a Pi Remote detached job. This is used by the Pi job watcher "
            "so the model does not poll job status."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta={**READ, "ui": {"visibility": ["app"]}},
    )
    async def wait_pi_job_completion(
        job_id: str,
        pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
        job_root: str | None = None,
        timeout_seconds: int = 90,
    ) -> dict[str, Any]:
        return await pi_job_command(
            _principal("remote:read"),
            "job-wait",
            job_id,
            pi_remote_dir,
            job_root,
            timeout_seconds=timeout_seconds,
        )

    @server.tool(
        name="bind_openai_pi_continuation",
        title="Bind Pi job to OpenAI session",
        description=(
            "Internal OpenAI runtime hook helper. Bind a watched Pi Remote job to the current "
            "Codex or ChatGPT Work session so the Stop hook can continue it after Pi finishes."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def bind_openai_pi_continuation(
        session_id: str,
        job_id: str,
        pi_remote_dir: str = "/home/ubuntu/src/pi-remote",
        job_root: str | None = None,
    ) -> dict[str, Any]:
        user_sub = _principal("remote:read")
        session_id = str(session_id).strip()
        job_id = str(job_id).strip()
        pi_remote_dir = str(pi_remote_dir).strip()
        if not session_id or len(session_id) > 256:
            raise ValueError("session_id must be a bounded non-empty string")
        if not job_id or len(job_id) > 128:
            raise ValueError("job_id must be a bounded non-empty string")
        if not pi_remote_dir or len(pi_remote_dir) > 4096:
            raise ValueError("pi_remote_dir must be a bounded non-empty path")
        if job_root is not None and len(job_root) > 4096:
            raise ValueError("job_root path is too long")
        binding = relay.store.bind_continuation(
            user_sub, session_id, job_id, pi_remote_dir, job_root
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"Pi Remote continuation is armed for job {binding['job_id']} in this OpenAI session."
                ),
            }
        }

    @server.tool(
        name="continue_openai_pi_job",
        title="Continue OpenAI session after Pi job",
        description=(
            "Internal OpenAI Stop-hook helper. Wait for the Pi Remote job bound to this session "
            "and return a Codex continuation decision when the job finishes."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        ),
        meta=READ,
    )
    async def continue_openai_pi_job(
        session_id: str,
        timeout_seconds: int = 540,
    ) -> dict[str, Any]:
        user_sub = _principal("remote:read")
        session_id = str(session_id).strip()
        if not session_id or len(session_id) > 256:
            raise ValueError("session_id must be a bounded non-empty string")
        binding = relay.store.continuation_for_user(user_sub, session_id)
        if binding is None:
            return {"continue": True}

        result = await wait_pi_job_until_terminal(user_sub, binding, timeout_seconds)
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        terminal = bool(result.get("terminal")) or state.get("status") in {
            "SUCCEEDED", "FAILED", "CANCELLED"
        }
        if terminal:
            relay.store.clear_continuation(user_sub, session_id)
            status = str(state.get("status") or "terminal")
            return {
                "decision": "block",
                "reason": (
                    f"Pi Remote job {binding['job_id']} completed with status {status}. "
                    "Continue this same development task now: inspect the durable Pi job/session "
                    "result, review changes and tests, then proceed to the next required step "
                    "without asking the user to say continue."
                ),
            }

        return {
            "decision": "block",
            "reason": (
                f"Pi Remote job {binding['job_id']} is still running after the bounded wait. "
                "Do not poll it from the model. End this continuation so the OpenAI Stop hook "
                "can resume waiting event-driven on the next stop."
            ),
        }

    def expose(name: str, read_only: bool, open_world: bool, destructive: bool):
        meta = READ if read_only else WRITE
        annotations = ToolAnnotations(
            readOnlyHint=read_only,
            openWorldHint=open_world,
            destructiveHint=destructive,
            idempotentHint=False if destructive else None,
        )
        title, description = TOOL_TEXT[name]
        return server.tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            meta=meta,
        )

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
        return await relay.call(_principal("remote:read"), "read_file", {"path": path, "project": project, "offset": offset, "max_bytes": max_bytes})

    @expose("list_dir", True, False, False)
    async def list_dir(path: str | None = None, project: str | None = None,
                       max_entries: int = 200) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "list_dir", {"path": path, "project": project, "max_entries": max_entries})

    @expose("logs", True, False, False)
    async def logs(unit: str | None = None, project: str | None = None, lines: int = 200,
                   since_minutes: int = 60) -> dict[str, Any]:
        return await relay.call(_principal("remote:read"), "logs", {"unit": unit, "project": project, "lines": lines, "since_minutes": since_minutes})

    @expose("write_file", False, False, True)
    async def write_file(path: str, content: str, project: str | None = None,
                         mode: str = "replace", expected_sha256: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "write_file", {"path": path, "content": content, "project": project, "mode": mode, "expected_sha256": expected_sha256})

    @expose("git", False, True, True)
    async def git(args: list[str], repo_path: str | None = None, project: str | None = None,
                  timeout_seconds: int = 30) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "git", {"args": args, "repo_path": repo_path, "project": project, "timeout_seconds": timeout_seconds})

    @expose("exec", False, True, True)
    async def exec(argv: list[str], cwd: str | None = None, project: str | None = None,
                   timeout_seconds: int = 30) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "exec", {"argv": argv, "cwd": cwd, "project": project, "timeout_seconds": timeout_seconds})

    @expose("process", False, False, True)
    async def process(action: str, pid: int | None = None, contains: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "process", {"action": action, "pid": pid, "contains": contains})

    @expose("systemd", False, False, True)
    async def systemd(action: str, unit: str | None = None, project: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "systemd", {"action": action, "unit": unit, "project": project})

    @expose("apply_patch", False, False, True)
    async def apply_patch(path: str, patch: str, expected_sha256: str | None = None,
                          project: str | None = None) -> dict[str, Any]:
        return await relay.call(_principal("remote:write"), "apply_patch", {"path": path, "patch": patch, "expected_sha256": expected_sha256, "project": project})

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

    def public_page(title: str, body_html: str) -> HTMLResponse:
        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · LivingRuntime Remote</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#111;color:#eee;margin:0}}
main{{max-width:760px;margin:7vh auto;padding:28px}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:24px;margin:18px 0}}
code,pre{{background:#0d0d0d;border-radius:8px;padding:10px;overflow:auto}}
pre{{white-space:pre-wrap}}
p,li{{color:#bbb;line-height:1.55}}
a{{color:#b9ccff}}
nav{{margin-bottom:28px}}
nav a{{margin-right:18px}}
</style>
</head>
<body><main>
<nav><a href="/install">Install</a><a href="/demo">Demo</a><a href="/support">Support</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a></nav>
{body_html}
</main></body></html>"""
        return HTMLResponse(
            body,
            headers={
                "cache-control": "public, max-age=300",
                "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
                "x-content-type-options": "nosniff",
            },
        )

    async def home(_: Request):
        return public_page(
            "LivingRuntime Remote",
            """<h1>LivingRuntime Remote</h1>
<p>Bounded remote development tools for machines and repositories you control.</p>
<div class="card"><p><a href="/install">Install the Connector</a></p>
<p><a href="/support">Support and documentation</a></p>\n<p><a href="/demo">Watch the review demo</a></p></div>""",
        )

    async def install_page(_: Request):
        origin = html.escape(public_origin)
        return public_page(
            "Install",
            f"""<h1>LivingRuntime Remote Connector</h1>
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
""",
        )

    async def privacy_page(_: Request):
        return public_page(
            "Privacy",
            """<h1>Privacy Policy</h1>
<p>LivingRuntime Remote connects ChatGPT-compatible clients to machines and repositories that the user is authorized to control.</p>
<div class="card"><h2>Data handled</h2>
<p>The public relay may process OAuth account identity, OAuth client metadata, hashed token values, pairing/device metadata, and the bounded tool inputs and outputs needed to route a request to the paired Connector. The Connector may also maintain non-secret SSH host aliases, allowed roots, project aliases, allowlisted service names, and local audit records.</p></div>
<div class="card"><h2>Credentials</h2>
<p>LivingRuntime Remote does not store SSH passwords or SSH private keys on the public relay. SSH credentials remain in the user's existing SSH configuration on the Connector machine. OAuth passwords are stored only as salted scrypt hashes, and raw OAuth bearer, refresh, and device tokens are not stored in relay databases.</p></div>
<div class="card"><h2>Sharing and retention</h2>
<p>When Remote is used with ChatGPT or another compatible client, tool arguments and results travel through that client's MCP request path and the LivingRuntime Remote relay. Successful relay task results are consumed after delivery; stale task records are eligible for cleanup after 24 hours. OAuth account and paired-device records remain until revoked or removed. Local configuration and audit logs remain on the user's machines until deleted.</p></div>
<p>ChatGPT-side retention follows the user's OpenAI account or workspace settings. Do not place secrets in tool arguments or file contents that you do not want transmitted through the connected client and relay.</p>""",
        )

    async def terms_page(_: Request):
        return public_page(
            "Terms",
            """<h1>Terms of Service</h1>
<p>LivingRuntime Remote is provided for operating machines, repositories, and services that you are authorized to access.</p>
<ol><li>Use Remote only with systems you are authorized to administer.</li>
<li>Remote is a bounded development execution channel, not a hostile-code sandbox.</li>
<li>Do not expose loopback MCP ports directly to the public internet.</li>
<li>ChatGPT plan, developer-mode, and marketplace permissions are controlled by OpenAI.</li></ol>
<p>The software is provided as-is, without warranty, subject to the repository license.</p>""",
        )

    async def support_page(_: Request):
        return public_page(
            "Support",
            """<h1>Support</h1>
<p>For setup problems, start by checking the paired Connector and calling <code>connection_status</code> and <code>capabilities</code> in ChatGPT.</p>
<div class="card"><h2>Expected healthy state</h2>
<p><code>connection_status.ok = true</code> and <code>capabilities.healthy = true</code> with an empty <code>missing</code> list.</p></div>
<div class="card"><h2>Installation</h2><p><a href="/install">Open the Connector installation page</a>.</p></div>
<p>LivingRuntime Remote source and issue tracking are published from the LivingRuntime Remote repository.</p>""",
        )

    async def demo_page(_: Request):
        return public_page(
            "Demo",
            """<h1>LivingRuntime Remote Demo</h1>
<p>This recorded demo shows the production OAuth connection and the isolated review workflow for connection status, project listing, bounded file access, Git status, and bounded writes.</p>
<div class="card"><p><a href="/demo.mp4">Open the MP4 recording</a></p></div>""",
        )

    async def demo_recording(_: Request):
        demo_path = Path(os.environ.get(
            "LIVINGRUNTIME_DEMO_RECORDING_PATH",
            "/var/lib/livingruntime-remote-relay/livingruntime-remote-demo.mp4",
        ))
        if not demo_path.exists():
            return PlainTextResponse("demo recording unavailable", status_code=404)
        return FileResponse(
            demo_path,
            media_type="video/mp4",
            headers={"cache-control": "public, max-age=3600"},
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
            device_id = device["device_id"]

            relay.arm_task_wait(device_id)
            task = store.claim(device_id)
            if task:
                return JSONResponse({"task": task})
            if wait <= 0:
                return JSONResponse({"task": None})

            if not await relay.wait_for_task(device_id, wait):
                return JSONResponse({"task": None})
            task = store.claim(device_id)
            return JSONResponse({"task": task})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)

    async def complete(request: Request):
        try:
            device = store.authenticate_device(_device_token(request))
            body = await request.json()
            task_id = str(body["task_id"])
            store.complete(device["device_id"], task_id, dict(body["result"]))
            relay.notify_result(task_id)
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
            "userinfo_endpoint": issuer + "/userinfo",
            "scopes_supported": SUPPORTED_SCOPES,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "subject_types_supported": ["public"],
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
            "resource_documentation": os.environ.get(
                "LIVINGRUNTIME_RELAY_DOCS_URL",
                os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/") + "/support",
            ),
            "resource_policy_uri": os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/") + "/privacy",
            "resource_tos_uri": os.environ["LIVINGRUNTIME_RELAY_ISSUER"].rstrip("/") + "/terms",
        }, headers={"cache-control": "no-store"})

    async def embedded_userinfo(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"www-authenticate": 'Bearer error="invalid_token"'},
            )
        if embedded_provider is None:
            return JSONResponse({"error": "server_error"}, status_code=500)
        token = embedded_provider.store.load_access_token(
            authorization.split(" ", 1)[1].strip()
        )
        if token is None:
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"www-authenticate": 'Bearer error="invalid_token"'},
            )
        if "openid" not in token.scopes:
            return JSONResponse(
                {"error": "insufficient_scope"},
                status_code=403,
                headers={"www-authenticate": 'Bearer error="insufficient_scope", scope="openid email"'},
            )
        info = embedded_provider.store.userinfo(token.subject)
        if info is None:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        if "email" not in token.scopes:
            info = {"sub": info["sub"]}
        return JSONResponse(info, headers={"cache-control": "no-store"})

    routes = [
        Route("/", home),
        Route("/healthz", health),
        Route("/install", install_page),
        Route("/install.ps1", install_ps1),
        Route("/install.sh", install_sh),
        Route("/download/{target}", download_connector),
        Route("/privacy", privacy_page),
        Route("/terms", terms_page),
        Route("/support", support_page),
        Route("/demo", demo_page),
        Route("/demo.mp4", demo_recording),
        Route("/.well-known/openai-apps-challenge", challenge),
    ]
    if _auth_mode() == "embedded":
        resource_path = urlsplit(os.environ["LIVINGRUNTIME_RELAY_RESOURCE_URL"]).path or ""
        routes.extend([
            Route("/.well-known/oauth-authorization-server", embedded_oauth_metadata),
            Route("/.well-known/openid-configuration", embedded_oauth_metadata),
            Route("/.well-known/oauth-protected-resource" + resource_path, embedded_resource_metadata),
            Route("/userinfo", embedded_userinfo),
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
