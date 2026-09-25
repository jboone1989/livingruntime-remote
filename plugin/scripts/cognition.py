from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

STORE_VERSION = 1
REQUEST_ID_RE = re.compile(r"^llmreq_[A-Za-z0-9._-]{1,120}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
WATCHER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
STATUSES = frozenset({"PENDING", "DISPATCHED", "COMPLETED", "TIMED_OUT"})
TERMINAL_STATUSES = frozenset({"COMPLETED", "TIMED_OUT"})
MAX_PAYLOAD_BYTES = 262144
MAX_RESPONSE_BYTES = 262144
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_CLAIM_SECONDS = 120


def cognition_root() -> Path:
    raw = str(os.environ.get("LIVINGRUNTIME_COGNITION_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".livingruntime" / "cognition"


def _requests_root() -> Path:
    return cognition_root() / "requests"


def _queue_lock_path() -> Path:
    return cognition_root() / ".queue.lock"


def _request_path(request_id: str) -> Path:
    return _requests_root() / (_validate_request_id(request_id) + ".json")


def _validate_request_id(request_id: str) -> str:
    value = str(request_id).strip()
    if not REQUEST_ID_RE.fullmatch(value):
        raise ValueError("request_id must start with llmreq_ and contain only bounded safe characters")
    return value


def _validate_agent_id(agent_id: str) -> str:
    value = str(agent_id).strip()
    if not AGENT_ID_RE.fullmatch(value):
        raise ValueError("agent_id must contain only letters, digits, dot, underscore, or dash")
    return value


def _validate_watcher_id(watcher_id: str) -> str:
    value = str(watcher_id).strip()
    if not WATCHER_ID_RE.fullmatch(value):
        raise ValueError("watcher_id must contain only bounded safe characters")
    return value


def _bounded_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    text = str(value)
    if required and not text.strip():
        raise ValueError(f"{field} is required")
    if len(text.encode("utf-8")) > maximum:
        raise ValueError(f"{field} is too large")
    return text


def _normalize_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    if len(messages) > 64:
        raise ValueError("messages contains too many entries")
    normalized: list[dict[str, str]] = []
    total = 0
    for index, row in enumerate(messages):
        if not isinstance(row, dict):
            raise ValueError(f"messages[{index}] must be an object")
        role = str(row.get("role") or "").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"messages[{index}].role is unsupported")
        content = _bounded_text(
            row.get("content"), field=f"messages[{index}].content", maximum=131072, required=True
        ) or ""
        total += len(content.encode("utf-8"))
        if total > MAX_PAYLOAD_BYTES:
            raise ValueError("message payload exceeds maximum size")
        normalized.append({"role": role, "content": content})
    return normalized


def _normalize_tools(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    if len(tools) > 64:
        raise ValueError("tools contains too many entries")
    normalized: list[dict[str, Any]] = []
    total = 0
    for index, row in enumerate(tools):
        if not isinstance(row, dict):
            raise ValueError(f"tools[{index}] must be an object")
        name = _bounded_text(
            row.get("name"), field=f"tools[{index}].name", maximum=256, required=True
        ) or ""
        description = _bounded_text(
            row.get("description") or "",
            field=f"tools[{index}].description",
            maximum=8192,
        ) or ""
        parameters = row.get("parameters") if row.get("parameters") is not None else {}
        if not isinstance(parameters, dict):
            raise ValueError(f"tools[{index}].parameters must be an object")
        try:
            encoded = json.dumps(
                parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError(f"tools[{index}].parameters must be JSON serializable") from None
        total += len(name.encode("utf-8")) + len(description.encode("utf-8")) + len(encoded)
        if total > 131072:
            raise ValueError("tool payload exceeds maximum size")
        normalized.append(
            {"name": name, "description": description, "parameters": json.loads(encoded)}
        )
    return normalized


def _normalize_response_format(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "text"}
    if not isinstance(value, dict):
        raise ValueError("response_format must be an object")
    kind = str(value.get("type") or "text").strip()
    if kind == "text":
        return {"type": "text"}
    if kind != "json_schema":
        raise ValueError("response_format.type must be text or json_schema")
    schema = value.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("json_schema response_format requires object schema")
    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 65536:
        raise ValueError("response schema exceeds maximum size")
    return {"type": "json_schema", "schema": schema}


def _normalize_options(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("options must be an object")
    allowed: dict[str, Any] = {}
    if value.get("max_output_tokens") is not None:
        tokens = int(value["max_output_tokens"])
        if not 1 <= tokens <= 32768:
            raise ValueError("max_output_tokens must be within 1..32768")
        allowed["max_output_tokens"] = tokens
    if value.get("temperature") is not None:
        temperature = float(value["temperature"])
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature must be within 0..2")
        allowed["temperature"] = temperature
    return allowed


def _normalize_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 32768:
        raise ValueError("metadata exceeds maximum size")
    return json.loads(encoded.decode("utf-8"))


def _canonical_payload(
    *,
    agent_id: str,
    purpose: str,
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]],
    response_format: dict[str, Any],
    options: dict[str, Any],
    metadata: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "purpose": purpose,
        "messages": messages,
        "tools": tools,
        "response_format": response_format,
        "options": options,
        "metadata": metadata,
        "timeout_seconds": timeout_seconds,
    }


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("LLM request payload exceeds maximum size")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _queue_lock() -> Iterator[None]:
    path = _queue_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            os.fchmod(handle.fileno(), 0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save(value: dict[str, Any]) -> None:
    path = _request_path(str(value["request_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _load(request_id: str) -> dict[str, Any]:
    path = _request_path(request_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KeyError(request_id) from exc
    if not isinstance(value, dict) or int(value.get("version", 0)) != STORE_VERSION:
        raise RuntimeError("invalid cognition request state")
    if value.get("request_id") != _validate_request_id(request_id):
        raise RuntimeError("cognition request identity mismatch")
    return value


def _refresh_timeout(value: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    if value.get("status") in TERMINAL_STATUSES:
        return value
    deadline = float(value.get("deadline_at") or 0.0)
    if deadline and now >= deadline:
        value["status"] = "TIMED_OUT"
        value["finished_at"] = now
        value["updated_at"] = now
        value["claim"] = None
        _save(value)
    return value


def submit(
    *,
    agent_id: str,
    purpose: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    request_id: str | None = None,
) -> dict[str, Any]:
    agent = _validate_agent_id(agent_id)
    purpose_text = _bounded_text(purpose, field="purpose", maximum=512, required=True) or ""
    normalized_messages = _normalize_messages(messages)
    normalized_tools = _normalize_tools(tools)
    normalized_format = _normalize_response_format(response_format)
    normalized_options = _normalize_options(options)
    normalized_metadata = _normalize_metadata(metadata)
    timeout = int(timeout_seconds)
    if not 5 <= timeout <= 3600:
        raise ValueError("timeout_seconds must be within 5..3600")
    payload = _canonical_payload(
        agent_id=agent,
        purpose=purpose_text,
        messages=normalized_messages,
        tools=normalized_tools,
        response_format=normalized_format,
        options=normalized_options,
        metadata=normalized_metadata,
        timeout_seconds=timeout,
    )
    digest = _payload_hash(payload)
    rid = _validate_request_id(request_id) if request_id else "llmreq_" + uuid.uuid4().hex[:24]
    now = time.time()
    with _queue_lock():
        try:
            existing = _load(rid)
        except KeyError:
            existing = None
        if existing is not None:
            if existing.get("payload_sha256") != digest:
                raise RuntimeError("request_id idempotency conflict")
            return _refresh_timeout(existing, now)
        value = {
            "version": STORE_VERSION,
            "request_id": rid,
            **payload,
            "payload_sha256": digest,
            "status": "PENDING",
            "claim": None,
            "attempts": 0,
            "response": None,
            "created_at": now,
            "updated_at": now,
            "deadline_at": now + timeout,
            "finished_at": None,
        }
        _save(value)
        return dict(value)


def get(request_id: str) -> dict[str, Any]:
    with _queue_lock():
        return dict(_refresh_timeout(_load(request_id)))


def get_status(request_id: str) -> dict[str, Any]:
    value = get(request_id)
    response = value.get("response") if isinstance(value.get("response"), dict) else {}
    claim = value.get("claim") if isinstance(value.get("claim"), dict) else {}
    return {
        "request_id": value["request_id"],
        "agent_id": value["agent_id"],
        "purpose": value["purpose"],
        "status": value["status"],
        "attempts": value.get("attempts", 0),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "deadline_at": value.get("deadline_at"),
        "finished_at": value.get("finished_at"),
        "claimed_by": claim.get("watcher_id"),
        "provider": response.get("provider"),
        "model": response.get("model"),
        "response_sha256": response.get("sha256"),
    }


def _candidate_rows(agent_id: str) -> list[dict[str, Any]]:
    root = _requests_root()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("llmreq_*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("agent_id") != agent_id:
            continue
        rows.append(value)
    rows.sort(key=lambda item: (float(item.get("created_at") or 0.0), str(item.get("request_id") or "")))
    return rows


def peek_next(*, agent_id: str) -> dict[str, Any] | None:
    """Read the next dispatchable request without mutating queue state."""
    agent = _validate_agent_id(agent_id)
    now = time.time()
    rows = _candidate_rows(agent)

    for row in rows:
        if row.get("status") != "DISPATCHED":
            continue
        deadline = float(row.get("deadline_at") or 0.0)
        if deadline and now >= deadline:
            continue
        claim = row.get("claim") if isinstance(row.get("claim"), dict) else {}
        if float(claim.get("expires_at") or 0.0) > now:
            return None

    for row in rows:
        deadline = float(row.get("deadline_at") or 0.0)
        if deadline and now >= deadline:
            continue
        if row.get("status") != "DISPATCHED":
            continue
        claim = row.get("claim") if isinstance(row.get("claim"), dict) else {}
        if float(claim.get("expires_at") or 0.0) <= now:
            return {
                "request_id": row.get("request_id"),
                "agent_id": row.get("agent_id"),
                "purpose": row.get("purpose"),
                "status": "DISPATCHED",
                "deadline_at": row.get("deadline_at"),
                "reclaimable": True,
            }

    for row in rows:
        deadline = float(row.get("deadline_at") or 0.0)
        if deadline and now >= deadline:
            continue
        if row.get("status") != "PENDING":
            continue
        return {
            "request_id": row.get("request_id"),
            "agent_id": row.get("agent_id"),
            "purpose": row.get("purpose"),
            "status": "PENDING",
            "deadline_at": row.get("deadline_at"),
            "reclaimable": False,
        }
    return None


def wait_pending(
    *,
    agent_id: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Bounded, side-effect-free wait used by the ChatGPT widget."""
    timeout = min(90, max(1, int(timeout_seconds)))
    deadline = time.monotonic() + timeout
    while True:
        request = peek_next(agent_id=agent_id)
        if request is not None:
            return {"request": request, "timed_out": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"request": None, "timed_out": True}
        time.sleep(min(0.5, remaining))


def claim_request(
    *,
    request_id: str,
    watcher_id: str,
    claim_seconds: int = DEFAULT_CLAIM_SECONDS,
) -> dict[str, Any]:
    """Atomically claim one request after a widget-created assistant turn starts."""
    rid = _validate_request_id(request_id)
    watcher = _validate_watcher_id(watcher_id)
    lease = int(claim_seconds)
    if not 15 <= lease <= 600:
        raise ValueError("claim_seconds must be within 15..600")
    now = time.time()
    with _queue_lock():
        value = _refresh_timeout(_load(rid), now)
        if value.get("status") == "COMPLETED":
            raise RuntimeError("cognition request already completed")
        if value.get("status") == "TIMED_OUT":
            raise RuntimeError("cognition request already timed out")

        for row in _candidate_rows(str(value.get("agent_id") or "")):
            if row.get("request_id") == rid or row.get("status") != "DISPATCHED":
                continue
            row_deadline = float(row.get("deadline_at") or 0.0)
            if row_deadline and now >= row_deadline:
                continue
            other_claim = row.get("claim") if isinstance(row.get("claim"), dict) else {}
            if float(other_claim.get("expires_at") or 0.0) > now:
                raise RuntimeError("another cognition request is already dispatched for this agent")

        if value.get("status") == "DISPATCHED":
            claim = value.get("claim") if isinstance(value.get("claim"), dict) else {}
            expires = float(claim.get("expires_at") or 0.0)
            if expires > now:
                if claim.get("watcher_id") == watcher:
                    return dict(value)
                raise RuntimeError("cognition request is already claimed")

        token = uuid.uuid4().hex
        value["status"] = "DISPATCHED"
        value["attempts"] = int(value.get("attempts") or 0) + 1
        value["claim"] = {
            "watcher_id": watcher,
            "token": token,
            "claimed_at": now,
            "expires_at": now + lease,
        }
        value["updated_at"] = now
        _save(value)
        return dict(value)


def claim_next(
    *,
    agent_id: str,
    watcher_id: str,
    claim_seconds: int = DEFAULT_CLAIM_SECONDS,
) -> dict[str, Any] | None:
    agent = _validate_agent_id(agent_id)
    watcher = _validate_watcher_id(watcher_id)
    lease = int(claim_seconds)
    if not 15 <= lease <= 600:
        raise ValueError("claim_seconds must be within 15..600")
    now = time.time()
    with _queue_lock():
        rows = _candidate_rows(agent)
        active_dispatch = False
        for row in rows:
            row = _refresh_timeout(row, now)
            if row.get("status") != "DISPATCHED":
                continue
            claim = row.get("claim") if isinstance(row.get("claim"), dict) else {}
            expires = float(claim.get("expires_at") or 0.0)
            if expires > now:
                active_dispatch = True
                break
            row["status"] = "PENDING"
            row["claim"] = None
            row["updated_at"] = now
            _save(row)
        if active_dispatch:
            return None
        for row in rows:
            row = _refresh_timeout(row, now)
            if row.get("status") != "PENDING":
                continue
            token = uuid.uuid4().hex
            row["status"] = "DISPATCHED"
            row["attempts"] = int(row.get("attempts") or 0) + 1
            row["claim"] = {
                "watcher_id": watcher,
                "token": token,
                "claimed_at": now,
                "expires_at": now + lease,
            }
            row["updated_at"] = now
            _save(row)
            return dict(row)
    return None


def wait_and_claim(
    *,
    agent_id: str,
    watcher_id: str,
    timeout_seconds: int = 30,
    claim_seconds: int = DEFAULT_CLAIM_SECONDS,
) -> dict[str, Any]:
    timeout = min(90, max(1, int(timeout_seconds)))
    deadline = time.monotonic() + timeout
    while True:
        request = claim_next(
            agent_id=agent_id,
            watcher_id=watcher_id,
            claim_seconds=claim_seconds,
        )
        if request is not None:
            return {"request": request, "timed_out": False}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"request": None, "timed_out": True}
        time.sleep(min(0.5, remaining))


def _normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("tool_calls must be a list with at most 64 entries")
    normalized: list[dict[str, Any]] = []
    total = 0
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError(f"tool_calls[{index}] must be an object")
        call_id = _bounded_text(row.get("id"), field=f"tool_calls[{index}].id", maximum=256, required=True) or ""
        name = _bounded_text(row.get("name"), field=f"tool_calls[{index}].name", maximum=256, required=True) or ""
        arguments = row.get("arguments") if row.get("arguments") is not None else {}
        if not isinstance(arguments, dict):
            raise ValueError(f"tool_calls[{index}].arguments must be an object")
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        total += len(call_id.encode("utf-8")) + len(name.encode("utf-8")) + len(encoded)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError("tool call payload exceeds maximum response size")
        normalized.append({"id": call_id, "name": name, "arguments": json.loads(encoded)})
    return normalized


def _response_digest(text: str, tool_calls: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        {"text": text, "tool_calls": tool_calls},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("response payload exceeds maximum size")
    return hashlib.sha256(encoded).hexdigest()


def complete(
    *,
    request_id: str,
    claim_token: str,
    response_text: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    provider: str = "livingruntime-chatgpt",
    model: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    text = _bounded_text(response_text or "", field="response_text", maximum=MAX_RESPONSE_BYTES) or ""
    normalized_tool_calls = _normalize_tool_calls(tool_calls)
    if not text.strip() and not normalized_tool_calls:
        raise ValueError("response must contain response_text or tool_calls")
    token = _bounded_text(claim_token, field="claim_token", maximum=128, required=True) or ""
    provider_name = _bounded_text(provider, field="provider", maximum=128, required=True) or ""
    model_name = _bounded_text(model, field="model", maximum=256)
    session = _bounded_text(session_id, field="session_id", maximum=256)
    digest = _response_digest(text, normalized_tool_calls)
    now = time.time()
    with _queue_lock():
        value = _refresh_timeout(_load(request_id), now)
        if value.get("status") == "COMPLETED":
            existing = value.get("response") if isinstance(value.get("response"), dict) else {}
            if existing.get("sha256") == digest:
                return dict(value)
            raise RuntimeError("completed request response conflict")
        if value.get("status") == "TIMED_OUT":
            raise RuntimeError("cognition request already timed out")
        if value.get("status") != "DISPATCHED":
            raise RuntimeError("cognition request is not dispatched")
        claim = value.get("claim") if isinstance(value.get("claim"), dict) else {}
        if claim.get("token") != token:
            raise RuntimeError("claim token mismatch")
        value["response"] = {
            "text": text,
            "tool_calls": normalized_tool_calls,
            "provider": provider_name,
            "model": model_name,
            "session_id": session,
            "sha256": digest,
            "completed_at": now,
        }
        value["status"] = "COMPLETED"
        value["finished_at"] = now
        value["updated_at"] = now
        value["claim"] = None
        _save(value)
        return dict(value)


def wait_response(request_id: str, *, timeout_seconds: int | None = None) -> dict[str, Any]:
    initial = get(request_id)
    requested = int(timeout_seconds) if timeout_seconds is not None else int(initial.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    if requested < 1:
        raise ValueError("timeout_seconds must be positive")
    deadline = time.monotonic() + requested
    while True:
        value = get(request_id)
        if value.get("status") == "COMPLETED":
            response = value.get("response")
            if not isinstance(response, dict):
                raise RuntimeError("completed cognition request has no response")
            return dict(value)
        if value.get("status") == "TIMED_OUT":
            raise TimeoutError("cognition request timed out")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("waiting for cognition response exceeded timeout")
        time.sleep(min(0.5, remaining))


def new_watcher_id(agent_id: str) -> str:
    agent = _validate_agent_id(agent_id)
    return f"watcher_{agent}_{uuid.uuid4().hex[:16]}"
