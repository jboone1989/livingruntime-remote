"""Stable mobile-chat entrypoint using LivingRuntime Remote's installed Claude adapter.

Called through the existing Remote exec tool; never starts Parley or handles
Claude credentials. The adapter runs Claude Code with its own native login.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import agent_policy
import agent_sessions
from agent_adapters import load_installed, registry

ADAPTER = "claude-code"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "STALLED"}


def _authorized(project: str, device: str) -> None:
    if not agent_policy.may_launch(device, project, ADAPTER):
        raise PermissionError("Claude adapter not allowlisted for this project and device")
    agent_policy.claude_workspace(device, project)


def _sessions() -> list[dict]:
    directory = Path(os.environ.get(
        "LIVINGRUNTIME_AGENT_SESSIONS", str(Path.home() / ".livingruntime" / "agent-sessions")
    ))
    found = []
    for path in sorted(directory.glob("lra_*.json"), reverse=True)[:200]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("adapter_id") == ADAPTER:
                found.append({key: record.get(key) for key in (
                    "session_id", "project", "device", "state", "created_at"
                )})
        except (OSError, ValueError, TypeError):
            continue
    return found[:100]


def _wait(session_id: str, seconds: int) -> dict:
    deadline = time.monotonic() + max(0, min(seconds, 20))
    status = agent_sessions.status(session_id)
    while str(status["agent_status"].get("status", "")).upper() not in TERMINAL:
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
        status = agent_sessions.status(session_id)
    return status


def run(args: argparse.Namespace) -> dict:
    if args.action in {"start", "ask"}:
        _authorized(args.project, args.device)
        record = agent_sessions.start(
            ADAPTER, args.message, project=args.project, device=args.device, authorized=True
        )
        if args.action == "start":
            return record
        return {**record, **_wait(record["session_id"], args.wait)}
    if args.action == "read":
        return agent_sessions.status(args.session)
    if args.action == "send":
        accepted = agent_sessions.send(args.session, args.message)
        return {**accepted, **_wait(args.session, args.wait)}
    if args.action == "stop":
        return agent_sessions.cancel(args.session)
    if args.action == "sessions":
        return {"sessions": _sessions()}
    raise ValueError("unknown action")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code via LivingRuntime Remote")
    parser.add_argument("action", choices=["ask", "start", "read", "send", "stop", "sessions"])
    parser.add_argument("--message")
    parser.add_argument("--session")
    parser.add_argument("--project", default="livingruntime-remote")
    parser.add_argument("--device", default="main")
    parser.add_argument("--wait", type=int, default=18)
    args = parser.parse_args()
    if args.action in {"ask", "start", "send"} and not (args.message or "").strip():
        parser.error("--message is required for ask/start/send")
    if args.action in {"read", "send", "stop"} and not args.session:
        parser.error("--session is required for read/send/stop")
    try:
        load_installed()
        registry.get(ADAPTER)
        result = run(args)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except (PermissionError, RuntimeError, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)},
                         ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
