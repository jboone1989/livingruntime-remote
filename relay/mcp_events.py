from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from mcp.types import RequestParams
from pydantic import Field


EVENT_NAME = "job.completed"
COGNITION_EVENT_NAME = "cognition.requested"
DEFAULT_TTL_MS = 24 * 60 * 60 * 1000
MIN_TTL_MS = 5 * 60 * 1000
MAX_TTL_MS = 24 * 60 * 60 * 1000
ROTATION_SECONDS = 300
MAX_EVENT_BYTES = 262_144


class EventsListParams(RequestParams):
    cursor: str | None = None


class EventsSubscribeParams(RequestParams):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: dict[str, Any]
    cursor: str | None = None
    ttl_ms: int | None = Field(default=None, alias="ttlMs")


class EventsUnsubscribeParams(RequestParams):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: dict[str, Any]


JOB_COMPLETED_DEFINITION = {
    "name": EVENT_NAME,
    "description": (
        "A durable LivingRuntime Remote long job reached a terminal state. "
        "Subscribe optionally by managed project or device."
    ),
    "delivery": ["webhook"],
    "inputSchema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "maxLength": 256},
            "device": {"type": "string", "maxLength": 256},
        },
        "additionalProperties": False,
    },
    "payloadSchema": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string"},
            "job_id": {"type": "string"},
            "status": {"type": "string"},
            "project": {"type": ["string", "null"]},
            "device": {"type": ["string", "null"]},
            "connector_id": {"type": "string"},
        },
        "required": [
            "event_id",
            "job_id",
            "status",
            "project",
            "device",
            "connector_id",
        ],
        "additionalProperties": False,
    },
}

COGNITION_REQUESTED_DEFINITION = {
    "name": COGNITION_EVENT_NAME,
    "description": (
        "A durable LivingRuntime cognition request is pending. "
        "The event is activation-only; authoritative prompt content remains in LivingRuntime."
    ),
    "delivery": ["webhook"],
    "inputSchema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "maxLength": 128},
            "lane": {"type": "string", "maxLength": 128},
        },
        "additionalProperties": False,
    },
    "payloadSchema": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string"},
            "request_id": {"type": "string"},
            "agent_id": {"type": "string"},
            "lane": {"type": "string"},
            "connector_id": {"type": "string"},
        },
        "required": ["event_id", "request_id", "agent_id", "lane", "connector_id"],
        "additionalProperties": False,
    },
}

EVENT_DEFINITIONS = {
    EVENT_NAME: JOB_COMPLETED_DEFINITION,
    COGNITION_EVENT_NAME: COGNITION_REQUESTED_DEFINITION,
}


class CallbackEndpointError(RuntimeError):
    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason


def canonical_arguments(
    arguments: dict[str, Any] | None,
    *,
    name: str = EVENT_NAME,
) -> tuple[dict[str, str], str]:
    raw = dict(arguments or {})
    allowed = (
        {"project", "device"}
        if name == EVENT_NAME
        else {"agent_id", "lane"}
        if name == COGNITION_EVENT_NAME
        else set()
    )
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported event arguments: {', '.join(sorted(unknown))}")
    normalized: dict[str, str] = {}
    for key in sorted(allowed):
        if key not in raw:
            continue
        value = str(raw[key]).strip()
        if not value or len(value) > 256:
            raise ValueError(f"{key} must be a non-empty string up to 256 characters")
        normalized[key] = value
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return normalized, canonical


def _secret_bytes(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("webhook signing secret must start with whsec_")
    encoded = secret[6:]
    try:
        raw = base64.b64decode(encoded + ("=" * (-len(encoded) % 4)), validate=True)
    except Exception as exc:
        raise ValueError("webhook signing secret is not valid base64") from exc
    if not 24 <= len(raw) <= 64:
        raise ValueError("webhook signing secret must decode to 24-64 bytes")
    return raw


def validate_delivery(delivery: dict[str, Any], *, require_secret: bool) -> tuple[str, str | None]:
    if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
        raise ValueError("delivery.mode must be webhook")
    url = str(delivery.get("url") or "").strip()
    _resolve_public_https(url)
    secret = delivery.get("secret")
    if require_secret:
        _secret_bytes(str(secret or ""))
        return url, str(secret)
    return url, None


def subscription_id(user_sub: str, callback_url: str, name: str, canonical_args: str) -> str:
    material = "\n".join((user_sub, callback_url, name, canonical_args)).encode()
    return "sub_" + hashlib.sha256(material).hexdigest()[:32]


def granted_expiry(ttl_ms: int | None, *, ttl_was_supplied: bool, now: float | None = None) -> float:
    now = time.time() if now is None else float(now)
    requested = DEFAULT_TTL_MS if (not ttl_was_supplied or ttl_ms is None) else int(ttl_ms)
    granted = min(MAX_TTL_MS, max(MIN_TTL_MS, requested))
    if ttl_was_supplied and ttl_ms is not None:
        granted = min(granted, int(ttl_ms))
    return now + (granted / 1000.0)


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_public_https(url: str) -> tuple[Any, list[str]]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise CallbackEndpointError("invalid_url", "callback URL must use https")
    if parts.username or parts.password or parts.fragment:
        raise CallbackEndpointError("invalid_url", "callback URL cannot contain credentials or fragments")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise CallbackEndpointError("invalid_url", "callback URL port is invalid") from exc
    if not (1 <= int(port) <= 65535):
        raise CallbackEndpointError("invalid_url", "callback URL port is invalid")
    try:
        infos = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CallbackEndpointError("dns_failed", str(exc)) from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise CallbackEndpointError("dns_failed", "callback DNS returned an invalid address") from exc
        if not ip.is_global:
            raise CallbackEndpointError("private_address", "callback resolves to a non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise CallbackEndpointError("dns_failed", "callback DNS returned no addresses")
    return parts, addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, connect_ip: str, port: int, timeout: float) -> None:
        self._connect_ip = connect_ip
        super().__init__(host=host, port=port, timeout=timeout, context=ssl.create_default_context())

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _post_signed(
    url: str,
    *,
    subscription_id_value: str,
    secret: str,
    previous_secret: str | None,
    previous_secret_until: float | None,
    webhook_id: str,
    body: bytes,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    parts, addresses = _resolve_public_https(url)
    if len(body) > MAX_EVENT_BYTES:
        raise ValueError("event payload exceeds 256 KiB")
    signed_at = int(time.time())
    signed = f"{webhook_id}.{signed_at}.".encode() + body
    signatures = []
    for candidate in (
        secret,
        previous_secret if previous_secret and (previous_secret_until or 0) > time.time() else None,
    ):
        if not candidate:
            continue
        digest = hmac.new(_secret_bytes(candidate), signed, hashlib.sha256).digest()
        signatures.append("v1," + base64.b64encode(digest).decode())
    headers = {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(signed_at),
        "webhook-signature": " ".join(signatures),
        "X-MCP-Subscription-Id": subscription_id_value,
    }
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    last_error: Exception | None = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(
            parts.hostname or "",
            address,
            parts.port or 443,
            timeout,
        )
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(65_537)
            if len(payload) > 65_536:
                raise CallbackEndpointError("response_too_large")
            return int(response.status), payload
        except Exception as exc:
            last_error = exc
        finally:
            connection.close()
    if isinstance(last_error, CallbackEndpointError):
        raise last_error
    raise CallbackEndpointError("connect_failed", str(last_error or "callback connection failed"))


def verify_callback(subscription: dict[str, Any]) -> None:
    challenge = "mcpv_" + secrets.token_urlsafe(24)
    webhook_id = "msg_verification_" + secrets.token_hex(12)
    body = json.dumps(
        {"type": "verification", "challenge": challenge},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    try:
        status, response = _post_signed(
            str(subscription["callback_url"]),
            subscription_id_value=str(subscription["subscription_id"]),
            secret=str(subscription["secret"]),
            previous_secret=None,
            previous_secret_until=None,
            webhook_id=webhook_id,
            body=body,
        )
    except CallbackEndpointError:
        raise
    except TimeoutError as exc:
        raise CallbackEndpointError("timeout", str(exc)) from exc
    if not 200 <= status < 300:
        raise CallbackEndpointError("challenge_failed", f"callback returned HTTP {status}")
    try:
        echoed = json.loads(response.decode())
    except Exception as exc:
        raise CallbackEndpointError("challenge_failed", "callback returned invalid JSON") from exc
    if not isinstance(echoed, dict) or not hmac.compare_digest(
        str(echoed.get("challenge") or ""),
        challenge,
    ):
        raise CallbackEndpointError("challenge_failed", "callback challenge did not match")


def event_matches(subscription: dict[str, Any], data: dict[str, Any]) -> bool:
    arguments = subscription.get("arguments")
    if not isinstance(arguments, dict):
        try:
            arguments = json.loads(str(subscription.get("arguments_json") or "{}"))
        except Exception:
            return False
    for key, expected in arguments.items():
        if expected is not None and str(data.get(key) or "") != str(expected):
            return False
    return True


def deliver_event(subscription: dict[str, Any], event: dict[str, Any]) -> int:
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
    status, _ = _post_signed(
        str(subscription["callback_url"]),
        subscription_id_value=str(subscription["subscription_id"]),
        secret=str(subscription["secret"]),
        previous_secret=(
            str(subscription["previous_secret"])
            if subscription.get("previous_secret")
            else None
        ),
        previous_secret_until=(
            float(subscription["previous_secret_until"])
            if subscription.get("previous_secret_until") is not None
            else None
        ),
        webhook_id=str(event["eventId"]),
        body=body,
    )
    return status


def validate_job_completed_event(payload: dict[str, Any], connector_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("name") != EVENT_NAME:
        raise ValueError("unsupported connector event")
    event_id = str(payload.get("eventId") or "").strip()
    if not event_id.startswith("lrcomp_") or len(event_id) > 128:
        raise ValueError("invalid completion event id")
    timestamp = str(payload.get("timestamp") or "").strip()
    if not timestamp or len(timestamp) > 64:
        raise ValueError("event timestamp is required")
    data = dict(payload.get("data") or {})
    if str(data.get("event_id") or "") != event_id:
        raise ValueError("completion event identity mismatch")
    job_id = str(data.get("job_id") or "").strip()
    status = str(data.get("status") or "").strip().upper()
    if not job_id or len(job_id) > 128:
        raise ValueError("invalid job id")
    if status not in {"SUCCEEDED", "FAILED", "CANCELLED", "CANCELLED_BY_USER"}:
        raise ValueError("invalid terminal job status")
    for key in ("project", "device"):
        if data.get(key) is not None:
            value = str(data[key])
            if len(value) > 256:
                raise ValueError(f"{key} is too long")
            data[key] = value
    data["connector_id"] = connector_id
    return {
        "eventId": event_id,
        "name": EVENT_NAME,
        "timestamp": timestamp,
        "data": {
            "event_id": event_id,
            "job_id": job_id,
            "status": status,
            "project": data.get("project"),
            "device": data.get("device"),
            "connector_id": connector_id,
        },
        "cursor": None,
    }


def validate_cognition_requested_event(
    payload: dict[str, Any],
    connector_id: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("name") != COGNITION_EVENT_NAME:
        raise ValueError("unsupported connector event")
    event_id = str(payload.get("eventId") or "").strip()
    if not event_id.startswith("lrcog_") or len(event_id) > 128:
        raise ValueError("invalid cognition event id")
    timestamp = str(payload.get("timestamp") or "").strip()
    if not timestamp or len(timestamp) > 64:
        raise ValueError("event timestamp is required")
    data = dict(payload.get("data") or {})
    if str(data.get("event_id") or "") != event_id:
        raise ValueError("cognition event identity mismatch")
    request_id = str(data.get("request_id") or "").strip()
    agent_id = str(data.get("agent_id") or "").strip()
    lane = str(data.get("lane") or "").strip()
    if not request_id.startswith("llmreq_") or len(request_id) > 128:
        raise ValueError("invalid cognition request id")
    if not agent_id or len(agent_id) > 128:
        raise ValueError("invalid cognition agent id")
    if not lane or len(lane) > 128:
        raise ValueError("invalid cognition lane")
    return {
        "eventId": event_id,
        "name": COGNITION_EVENT_NAME,
        "timestamp": timestamp,
        "data": {
            "event_id": event_id,
            "request_id": request_id,
            "agent_id": agent_id,
            "lane": lane,
            "connector_id": connector_id,
        },
        "cursor": None,
    }


def validate_connector_event(payload: dict[str, Any], connector_id: str) -> dict[str, Any]:
    name = str(payload.get("name") or "") if isinstance(payload, dict) else ""
    if name == EVENT_NAME:
        return validate_job_completed_event(payload, connector_id)
    if name == COGNITION_EVENT_NAME:
        return validate_cognition_requested_event(payload, connector_id)
    raise ValueError("unsupported connector event")
