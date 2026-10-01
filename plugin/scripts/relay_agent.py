from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import bridge
import cognition
import jobs
from contract import PLUGIN_VERSION, REMOTE_TOOLS

TOOLS = set(REMOTE_TOOLS)
_EVENT_FORWARD_LOCK = threading.Lock()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("relay redirects are not allowed")


def _check_url(value: str) -> str:
    url = value.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("relay URL must use HTTPS")
    return url


def _request(base: str, path: str, body: dict[str, Any],
             token: str | None = None, timeout: int = 35) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode()
    headers = {
        "content-type": "application/json",
        "user-agent": f"LivingRuntime-Remote/{PLUGIN_VERSION}",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
        payload = json.loads(response.read(1048576).decode())
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload


def _config_path(value: str | None) -> Path:
    return Path(value or os.environ.get(
        "LIVINGRUNTIME_RELAY_CONFIG",
        str(Path.home() / ".livingruntime" / "relay.json"),
    )).expanduser()


def pair(base: str, code: str, name: str, config_path: Path) -> dict[str, Any]:
    result = _request(base, "/device/pair", {"code": code, "name": name})
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({
        "url": base,
        "device_id": result["device_id"],
        "device_token": result["device_token"],
        "name": name,
    }, indent=2) + "\n", encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    return {"device_id": result["device_id"], "config": str(config_path)}


def _load(config_path: Path) -> dict[str, Any]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    cfg["url"] = _check_url(str(cfg["url"]))
    if not cfg.get("device_token"):
        raise RuntimeError("relay config is missing device_token")
    return cfg


def _dispatch(task: dict[str, Any]) -> dict[str, Any]:
    tool = str(task.get("tool", ""))
    if tool not in TOOLS:
        return {"ok": False, "error": f"relay tool is not allowlisted: {tool}"}
    try:
        fn = getattr(bridge, tool)
        result = fn(**dict(task.get("args") or {}))
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}


def _event_timestamp(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _forward_completion_events(cfg: dict[str, Any]) -> bool:
    if not _EVENT_FORWARD_LOCK.acquire(blocking=False):
        return False
    delivered_any = False
    try:
        for job in jobs.pending_completion_events(limit=50):
            event = job.get("completion_event")
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id:
                continue
            payload = {
                "eventId": event_id,
                "name": "job.completed",
                "timestamp": _event_timestamp(
                    float(event.get("created_at") or job.get("updated_at") or time.time())
                ),
                "data": {
                    "event_id": event_id,
                    "job_id": str(job.get("job_id") or ""),
                    "status": str(job.get("status") or ""),
                    "project": job.get("project"),
                    "device": job.get("device"),
                },
                "cursor": None,
            }
            try:
                result = _request(
                    cfg["url"],
                    "/device/event",
                    payload,
                    cfg["device_token"],
                    timeout=15,
                )
            except Exception as exc:
                print(
                    f"relay event delivery error: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue
            matching = int(result.get("matching_subscriptions") or 0)
            delivered = int(result.get("delivered") or 0)
            if result.get("ok") is True and matching > 0 and delivered > 0:
                jobs.mark_completion_event_forwarded(
                    str(job["job_id"]),
                    event_id,
                )
                delivered_any = True
                continue
            # No matching subscriber means this event cannot make progress
            # right now. Stop this batch instead of hammering the relay with
            # the same durable event dozens of times.
            if matching == 0 or delivered == 0:
                break
    finally:
        _EVENT_FORWARD_LOCK.release()
    return delivered_any


def _forward_cognition_events(cfg: dict[str, Any]) -> bool:
    if not _EVENT_FORWARD_LOCK.acquire(blocking=False):
        return False
    delivered_any = False
    try:
        for request in cognition.pending_activation_events(limit=50):
            event = request.get("activation_event")
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id:
                continue
            payload = {
                "eventId": event_id,
                "name": "cognition.requested",
                "timestamp": _event_timestamp(
                    float(event.get("created_at") or request.get("created_at") or time.time())
                ),
                "data": {
                    "event_id": event_id,
                    "request_id": str(request.get("request_id") or ""),
                    "agent_id": str(request.get("agent_id") or ""),
                    "lane": cognition.dispatch_lane(request),
                },
                "cursor": None,
            }
            try:
                result = _request(
                    cfg["url"],
                    "/device/event",
                    payload,
                    cfg["device_token"],
                    timeout=15,
                )
            except Exception as exc:
                print(
                    f"relay cognition event delivery error: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue
            matching = int(result.get("matching_subscriptions") or 0)
            delivered = int(result.get("delivered") or 0)
            if result.get("ok") is True and matching > 0 and delivered > 0:
                cognition.mark_activation_event_forwarded(
                    str(request["request_id"]),
                    event_id,
                )
                delivered_any = True
                continue
            if matching == 0 or delivered == 0:
                break
    finally:
        _EVENT_FORWARD_LOCK.release()
    return delivered_any


def _forward_events(cfg: dict[str, Any]) -> bool:
    completion_delivered = _forward_completion_events(cfg)
    cognition_delivered = _forward_cognition_events(cfg)
    return completion_delivered or cognition_delivered


def _handle_claimed_task(cfg: dict[str, Any], task: dict[str, Any]) -> None:
    result = _dispatch(task)
    _request(
        cfg["url"],
        "/device/result",
        {"task_id": task["task_id"], "result": result},
        cfg["device_token"],
        timeout=30,
    )


def serve(config_path: Path, once: bool = False) -> None:
    cfg = _load(config_path)
    max_workers = min(
        8,
        max(2, int(os.environ.get("LIVINGRUNTIME_CONNECTOR_WORKERS", "4"))),
    )
    pending: set[concurrent.futures.Future[None]] = set()
    event_future: concurrent.futures.Future[bool] | None = None
    next_event_forward_at = 0.0
    event_retry_seconds = max(
        5.0,
        float(os.environ.get("LIVINGRUNTIME_EVENT_RETRY_SECONDS", "30")),
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="livingruntime-remote",
    ) as pool:
        while True:
            try:
                finished = {future for future in pending if future.done()}
                for future in finished:
                    pending.remove(future)
                    future.result()

                if event_future is not None and event_future.done():
                    delivered = bool(event_future.result())
                    event_future = None
                    next_event_forward_at = time.monotonic() + (
                        1.0 if delivered else event_retry_seconds
                    )

                # Tool execution is the primary connector lane. Pending MCP
                # events must never block polling, especially when the Work
                # host has not installed a matching events/subscribe callback.
                response = _request(
                    cfg["url"],
                    "/device/poll?wait=20",
                    {},
                    cfg["device_token"],
                    timeout=30,
                )
                task = response.get("task")
                if task:
                    future = pool.submit(_handle_claimed_task, cfg, task)
                    pending.add(future)
                    if once:
                        future.result()
                        return

                # Event delivery is best-effort and independent from task
                # polling. Only one forwarder runs at a time so a backlog
                # cannot consume the connector worker pool.
                if (
                    event_future is None
                    and time.monotonic() >= next_event_forward_at
                ):
                    event_future = pool.submit(_forward_events, cfg)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(f"relay connector error: {type(exc).__name__}: {exc}", flush=True)
                if once:
                    raise
                time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="LivingRuntime Remote public relay connector")
    parser.add_argument("--url", help="Public LivingRuntime Remote relay base URL")
    parser.add_argument("--pair", metavar="CODE", help="One-time pairing code from ChatGPT")
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--config")
    parser.add_argument("--once", action="store_true", help="Handle one task, then exit")
    args = parser.parse_args()
    path = _config_path(args.config)
    if args.pair:
        if not args.url:
            parser.error("--url is required with --pair")
        result = pair(_check_url(args.url), args.pair, args.name, path)
        print(json.dumps(result, indent=2))
        return
    serve(path, once=args.once)


if __name__ == "__main__":
    main()
