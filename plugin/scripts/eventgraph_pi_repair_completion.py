"""Repair completion receipts on the existing cn-dev Connector polling loop.

No new scheduler and no GitHub credential. Only a successful native Pi session
may export an isolated worktree to the Relay's owner-bound patch inbox.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import agent_sessions
from eventgraph_pi_patch_export import export

MARKER = re.compile(r"^\[eventgraph-pi-repair pr=([1-9][0-9]*) sha=([0-9a-f]{40})\] ")
ROOT = Path("/home/ubuntu/livingruntime-aihot/.worktrees")


def journal_dir():
    return Path(os.environ.get("LIVINGRUNTIME_PI_REPAIR_JOURNALS",
                               str(Path.home() / ".livingruntime" / "pi-repairs")))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        json.dump(data, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def register(task, result):
    if task.get("tool") != "start_agent" or not result.get("ok"):
        return
    args = task.get("args") or {}
    if args.get("adapter_id") != "pi" or args.get("project") != "aihot-dev":
        return
    match = MARKER.match(str(args.get("goal") or ""))
    if not match:
        return
    number, sha = int(match.group(1)), match.group(2)
    session = (result.get("result") or {}).get("session_id")
    if not isinstance(session, str) or not session.startswith("lra_"):
        raise ValueError("repair launch has no native session")
    native = agent_sessions.get(session)
    if native.get("adapter_id") != "pi" or native.get("project") != "aihot-dev":
        raise PermissionError("unexpected native repair session")
    path = journal_dir() / (session + ".json")
    item = {"session_id": session, "pr_number": number, "sha": sha, "state": "RUNNING",
            "worktree": str(ROOT / f"pi-repair-{number}-{sha[:12]}")}
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != item:
            raise PermissionError("conflicting repair journal")
        return
    save(path, item)


def deliver(cfg, request):
    if cfg.get("name") != "cn-dev":
        return False
    delivered = False
    completed = 0
    for path in sorted(journal_dir().glob("lra_*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item["state"] != "RUNNING":
            continue
        try:
            observed = agent_sessions.status(item["session_id"])
            status = str(observed["agent_status"].get("status", "UNKNOWN")).upper()
            if status not in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED", "STALLED"}:
                continue
            # Limit actual completions, not the first three filenames. Old
            # DELIVERED/BLOCKED or still-running sessions must not starve new
            # successful Pi repairs forever.
            if completed >= 3:
                break
            completed += 1
            if status != "SUCCEEDED":
                save(path, {**item, "state": "BLOCKED", "reason": "native-" + status})
                continue
            payload = export(item["worktree"], item["pr_number"], item["sha"])
            receipt = request(cfg["url"], "/device/pi-repair-result",
                              {"session_id": item["session_id"], "manifest": payload},
                              cfg["device_token"], timeout=30)
            if receipt.get("ok") is not True:
                raise RuntimeError("Relay declined Pi repair handoff")
            save(path, {**item, "state": "DELIVERED"})
            delivered = True
        except (ValueError, PermissionError, UnicodeError) as exc:
            # A successful Pi session with an empty, forbidden or malformed
            # patch cannot improve on retry. Preserve the receipt and expose
            # a human blocker instead of retrying the same bad patch forever.
            save(path, {**item, "state": "BLOCKED", "reason": "invalid-patch:" + type(exc).__name__})
        except Exception as exc:
            # Transport outages and transient Git failures remain retryable;
            # never convert either path into a green review/merge result.
            print("Pi repair completion deferred: " + type(exc).__name__, flush=True)
    return delivered
