"""Native Claude Code print-mode runner with streamed, durable public events.

Uses the CLI's own tools, skills, agents, hooks, and MCP configuration.
No second agent engine is implemented here.
"""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def cli_args(record):
    opts = record.get("options") or {}
    args = [record["executable"], "-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--permission-mode", opts.get("permission_mode", "plan")]
    for key in ("agent", "model", "effort"):
        if opts.get(key):
            args += ["--" + key, opts[key]]
    if opts.get("agents"):
        args += ["--agents", json.dumps(opts["agents"], ensure_ascii=False)]
    for key, flag in (
        ("append_system_prompt", "--append-system-prompt"),
        ("append_subagent_system_prompt", "--append-subagent-system-prompt"),
        ("fallback_model", "--fallback-model"),
        ("autocompact", "--autocompact"),
    ):
        if opts.get(key):
            args += [flag, opts[key]]
    for key, flag in (
        ("forward_subagent_text", "--forward-subagent-text"),
        ("include_hook_events", "--include-hook-events"),
    ):
        if opts.get(key):
            args.append(flag)
    if opts.get("max_budget_usd"):
        args += ["--max-budget-usd", str(opts["max_budget_usd"])]
    if opts.get("json_schema"):
        args += ["--json-schema", json.dumps(opts["json_schema"])]
    if opts.get("mcp_configs"):
        args += ["--mcp-config", *opts["mcp_configs"]]
    if opts.get("additional_dirs"):
        args += ["--add-dir", *opts["additional_dirs"]]
    for directory in opts.get("plugin_dirs", []):
        args += ["--plugin-dir", directory]
    if opts.get("max_turns"):
        args += ["--max-turns", str(opts["max_turns"])]
    for key, flag in (("allowed_tools", "--allowedTools"), ("disallowed_tools", "--disallowedTools"),
                      ("tools", "--tools")):
        if opts.get(key):
            # The equals form avoids variadic flags consuming the following option.
            args.append(flag + "=" + ",".join(opts[key]))
    if opts.get("duplex"):
        args += ["--input-format", "stream-json", "--permission-prompt-tool", "stdio"]
    args += ["--resume" if record.get("continue") else "--session-id", record["session_id"]]
    if not opts.get("duplex"):
        # Protect untrusted prompt text beginning with CLI-style flags.
        args += ["--", record["prompt"]]
    return args


def public_events(raw):
    """Bounded developer-visible metadata, without raw tool arguments or tool output."""
    kind = raw.get("type")
    subtype = raw.get("subtype")
    parent = raw.get("parent_tool_use_id")
    context = {"parent_tool_use_id": str(parent)[:128]} if isinstance(parent, str) and parent else {}
    if kind == "system":
        if subtype == "init":
            event = {"kind": "session", "model": str(raw.get("model", ""))[:100]}
            servers = raw.get("mcp_servers")
            if isinstance(servers, list):
                event["mcp_servers"] = [
                    {"name": str(x.get("name", ""))[:100], "status": str(x.get("status", ""))[:60]}
                    for x in servers[:30] if isinstance(x, dict)
                ]
            for field in ("skills", "agents"):
                items = raw.get(field)
                if isinstance(items, list):
                    event[field] = [str(x)[:100] for x in items[:50] if isinstance(x, str)]
            return [event]
        if subtype in ("hook_started", "hook_progress", "hook_response"):
            return [{"kind": subtype, "hook_name": str(raw.get("hook_name", raw.get("hook_id", "")))[:100], **context}]
        if subtype in ("task_started", "task_progress", "task_notification", "api_retry", "compact_boundary"):
            return [{"kind": subtype, "task_id": str(raw.get("task_id", ""))[:128], **context}]
    if kind == "stream_event":
        ev = raw.get("event") or {}
        delta = ev.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [{"kind": "text_delta", "text": str(delta["text"])[:4000], **context}]
        return []
    if kind == "assistant":
        result = []
        for item in (raw.get("message") or {}).get("content", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                event = {"kind": "tool_call", "name": str(item.get("name", "Tool"))[:100],
                         "tool_id": str(item.get("id", ""))[:128], **context}
                detail = item.get("input")
                if isinstance(detail, dict):
                    if item.get("name") == "Skill" and isinstance(detail.get("skill"), str):
                        event["skill"] = detail["skill"][:100]
                    elif item.get("name") in ("Agent", "Task") and isinstance(detail.get("subagent_type"), str):
                        event["subagent_type"] = detail["subagent_type"][:100]
                result.append(event)
            elif item.get("type") == "text" and item.get("text"):
                result.append({"kind": "assistant_text", "text": str(item["text"])[:4000], **context})
        return result[:30]
    if kind in ("hook_started", "hook_progress", "hook_response"):
        # Older CLI versions also emitted some hook events as top-level types.
        return [{"kind": kind, "hook_name": str(raw.get("hook_name", raw.get("hook_id", "")))[:100], **context}]
    if kind == "user":
        results = []
        for item in (raw.get("message") or {}).get("content", []):
            if isinstance(item, dict) and item.get("type") == "tool_result":
                results.append({"kind": "tool_result", "tool_id": str(item.get("tool_use_id", ""))[:128],
                                "success": not item.get("is_error", False), **context})
        return results[:30]
    if kind == "control_request":
        req = raw.get("request") or {}
        if not isinstance(req, dict):
            return []
        rid = raw.get("request_id")
        if not isinstance(rid, str) or not rid or len(rid) > 128:
            return []
        tool_input = req.get("input") if isinstance(req.get("input"), dict) else {}
        approvable = len(json.dumps(tool_input)) <= 12000
        if not approvable:
            tool_input = {"truncated": True}
        return [{"kind": "permission_request", "request_id": rid,
                 "tool_name": str(req.get("tool_name", ""))[:100],
                 "approvable": approvable, "input": tool_input}]
    if kind == "control_cancel_request":
        return [{"kind": "permission_cancelled", "request_id": str(raw.get("request_id", ""))[:128]}]
    if kind == "result":
        return [{"kind": "completion", "success": not bool(raw.get("is_error")),
                 "turns": raw.get("num_turns"), "duration_ms": raw.get("duration_ms")}]
    return []

def terminate_cli_group(proc):
    """Ask native Claude to stop, then reap any Bash/MCP children in its group."""
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Even when Claude exits promptly, a child may still hold stdout open.
    # Always clear remaining members of the isolated Claude process group.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def main(path):
    record_path = Path(path)
    receipt_path = record_path.with_suffix(".result.json")
    events_path = record_path.with_suffix(".events.jsonl")
    decisions_path = record_path.with_suffix(".decisions.jsonl")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    output = {"status": "FAILED", "finished_at": time.time()}
    proc = None
    killed = False
    # A session has multiple turns; event cursors never restart at one.
    seq = 0
    if events_path.exists():
        with events_path.open(encoding="utf-8") as previous_events:
            for line in previous_events:
                try:
                    seq = max(seq, int(json.loads(line).get("seq", 0)))
                except (ValueError, TypeError):
                    continue
    stop_monitor = threading.Event()
    stdin_lock = threading.Lock()
    timeout_lock = threading.Lock()
    pending_approvals = {}
    deadline = [0.0]

    try:
        opts = record.get("options") or {}
        timeout = opts.get("timeout_seconds", 900)
        approval_timeout = opts.get("approval_timeout_seconds", 3600)
        deadline[0] = time.monotonic() + timeout
        args = cli_args(record)
        with events_path.open("a", encoding="utf-8") as events:
            os.chmod(events_path, 0o600)

            def append(event):
                nonlocal seq
                seq += 1
                events.write(json.dumps({"seq": seq, **event}, ensure_ascii=False) + "\n")
                events.flush()

            proc = subprocess.Popen(
                args, cwd=record["cwd"], stdin=subprocess.PIPE if opts.get("duplex") else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                start_new_session=True,
            )
            stderr_parts = []
            def collect_stderr():
                for chunk in proc.stderr:
                    if sum(map(len, stderr_parts)) < 4000:
                        stderr_parts.append(chunk)
            stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
            stderr_thread.start()
            if threading.current_thread() is threading.main_thread():
                def on_worker_term(_signum, _frame):
                    # Native cancel targets the worker's original process group.
                    # Claude lives in another group so its own children can be reaped.
                    terminate_cli_group(proc)
                    raise SystemExit(143)
                signal.signal(signal.SIGTERM, on_worker_term)
            if opts.get("duplex"):
                message = {"type": "user", "message": {"role": "user", "content": record["prompt"]}}
                with stdin_lock:
                    proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                    proc.stdin.flush()

            def apply_decisions():
                offset = 0
                while not stop_monitor.wait(0.15):
                    if not decisions_path.exists() or proc.poll() is not None:
                        continue
                    try:
                        with decisions_path.open(encoding="utf-8") as f:
                            f.seek(offset)
                            while True:
                                line = f.readline()
                                if not line:
                                    break
                                try:
                                    item = json.loads(line)
                                except ValueError:
                                    continue
                                response = ({"behavior": "allow", "updatedInput": item.get("input", {})}
                                            if item.get("decision") == "allow" else
                                            {"behavior": "deny", "message": "Denied by operator"})
                                control = {"type": "control_response", "response": {
                                    "subtype": "success", "request_id": item["request_id"],
                                    "response": response}}
                                with stdin_lock:
                                    if proc.stdin.closed:
                                        return
                                    proc.stdin.write(json.dumps(control) + "\n")
                                    proc.stdin.flush()
                                with timeout_lock:
                                    pending_approvals.pop(item["request_id"], None)
                                    deadline[0] = time.monotonic() + timeout
                            offset = f.tell()
                    except (OSError, BrokenPipeError, ValueError):
                        return

            if opts.get("duplex"):
                threading.Thread(target=apply_decisions, daemon=True).start()

            def monitor_timeout():
                nonlocal killed
                # User approval is not model compute time. Pause the active-work
                # deadline while any native CLI tool request awaits an answer.
                while not stop_monitor.wait(0.25):
                    with timeout_lock:
                        now = time.monotonic()
                        expired = ((not pending_approvals and now >= deadline[0]) or
                                   (bool(pending_approvals) and now - min(pending_approvals.values()) >= approval_timeout))
                    if expired:
                        killed = True
                        terminate_cli_group(proc)
                        return

            threading.Thread(target=monitor_timeout, daemon=True).start()
            last_result = None
            try:
                for line in proc.stdout:
                    if len(line) > 20_000_000:
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    # Inactivity rather than total wall time: active compilation,
                    # tool events and model streaming should keep a task alive.
                    # Do not reset the deadline while waiting for human approval.
                    with timeout_lock:
                        if not pending_approvals:
                            deadline[0] = time.monotonic() + timeout
                    if raw.get("type") == "result":
                        last_result = raw
                    for event in public_events(raw):
                        # Register BEFORE publication: an external client may approve
                        # immediately after seeing the durable event.
                        if event["kind"] == "permission_request" and opts.get("duplex") and event["approvable"]:
                            with timeout_lock:
                                pending_approvals[event["request_id"]] = time.monotonic()
                        append(event)
                        if event["kind"] == "permission_cancelled":
                            with timeout_lock:
                                pending_approvals.pop(event["request_id"], None)
                                deadline[0] = time.monotonic() + timeout
                        if (event["kind"] == "permission_request" and
                                not event["approvable"] and opts.get("duplex")):
                            deny = {"type": "control_response", "response": {
                                "subtype": "success", "request_id": event["request_id"],
                                "response": {"behavior": "deny", "message": "Permission input exceeds review limit"}}}
                            with stdin_lock:
                                proc.stdin.write(json.dumps(deny) + "\n")
                                proc.stdin.flush()
                    if raw.get("type") == "result" and opts.get("duplex"):
                        # Claude waits for EOF in stream-json input mode.
                        stop_monitor.set()
                        with stdin_lock:
                            if not proc.stdin.closed:
                                proc.stdin.close()
                returncode = proc.wait()
                stderr_thread.join(timeout=1)
                stderr = "".join(stderr_parts)[-1500:]
            finally:
                stop_monitor.set()
                if proc.poll() is None:
                    terminate_cli_group(proc)
            if killed:
                output.update({"status": "STALLED", "error": "Claude task timed out"})
            elif returncode:
                output["error"] = "Claude exit=%s: %s" % (returncode, stderr.strip() or str((last_result or {}).get("result", ""))[:500])
            elif not isinstance(last_result, dict):
                output["error"] = "Claude returned no result event"
            elif last_result.get("is_error") or str(last_result.get("subtype", "")).startswith("error_"):
                output["error"] = "Claude: " + str(last_result.get("result") or last_result.get("subtype") or "unknown error")[-800:]
            elif ((not isinstance(last_result.get("result"), str) or not last_result["result"].strip())
                  and not isinstance(last_result.get("structured_output"), (dict, list))):
                output["error"] = "Claude returned no result text"
            else:
                output["status"] = "SUCCEEDED"
                output["result"] = str(last_result.get("result") or "")[:80000]
                if isinstance(last_result.get("structured_output"), (dict, list)):
                    output["structured_output"] = last_result["structured_output"]
                output["usage"] = {"num_turns": last_result.get("num_turns"),
                                   "total_cost_usd": last_result.get("total_cost_usd"),
                                   "duration_ms": last_result.get("duration_ms"),
                                   "usage": last_result.get("usage")}
    except (ValueError, OSError, KeyError) as exc:
        output["error"] = type(exc).__name__ + ": " + str(exc)[:300]
    finally:
        stop_monitor.set()
        output["finished_at"] = time.time()
        tmp = receipt_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            json.dump(output, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, receipt_path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    main(sys.argv[1])
