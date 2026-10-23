"""Native Claude Code print-mode runner with streamed, durable public events.

Uses the CLI's own tools, skills, agents, hooks, and MCP configuration.
No second agent engine is implemented here.
"""
from __future__ import annotations
import json
import os
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
    kind = raw.get("type")
    if kind == "system" and raw.get("subtype") == "init":
        return [{"kind": "session", "model": str(raw.get("model", ""))[:100]}]
    if kind == "stream_event":
        ev = raw.get("event") or {}
        delta = ev.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [{"kind": "text_delta", "text": str(delta["text"])[:4000]}]
        return []
    if kind == "assistant":
        result = []
        for item in (raw.get("message") or {}).get("content", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                result.append({"kind": "tool_call", "name": str(item.get("name", "Tool"))[:100],
                               "tool_id": str(item.get("id", ""))[:128]})
            elif item.get("type") == "text" and item.get("text"):
                result.append({"kind": "assistant_text", "text": str(item["text"])[:4000]})
        return result[:30]
    if kind == "control_request":
        req = raw.get("request") or {}
        if not isinstance(req, dict):
            return []
        rid = raw.get("request_id")
        if not isinstance(rid, str) or not rid or len(rid) > 128:
            return []
        # Include the requested input so the human can make an informed decision.
        tool_input = req.get("input") if isinstance(req.get("input"), dict) else {}
        approvable = len(json.dumps(tool_input)) <= 12000
        if not approvable:
            # Never permit a truncated preview to become the executed tool input.
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

    try:
        opts = record.get("options") or {}
        timeout = opts.get("timeout_seconds", 900)
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
            )
            stderr_parts = []
            def collect_stderr():
                for chunk in proc.stderr:
                    if sum(map(len, stderr_parts)) < 4000:
                        stderr_parts.append(chunk)
            stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
            stderr_thread.start()
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
                            offset = f.tell()
                    except (OSError, BrokenPipeError, ValueError):
                        return

            if opts.get("duplex"):
                threading.Thread(target=apply_decisions, daemon=True).start()

            def stop_timeout():
                nonlocal killed
                if proc.poll() is None:
                    killed = True
                    proc.kill()

            timer = threading.Timer(timeout, stop_timeout)
            timer.daemon = True
            timer.start()
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
                    if raw.get("type") == "result":
                        last_result = raw
                    for event in public_events(raw):
                        append(event)
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
                timer.cancel()
                stop_monitor.set()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if killed:
                output.update({"status": "STALLED", "error": "Claude task timed out"})
            elif returncode:
                output["error"] = "Claude exit=%s: %s" % (returncode, stderr.strip() or str((last_result or {}).get("result", ""))[:500])
            elif not isinstance(last_result, dict):
                output["error"] = "Claude returned no result event"
            elif last_result.get("is_error") or str(last_result.get("subtype", "")).startswith("error_"):
                output["error"] = "Claude: " + str(last_result.get("result") or last_result.get("subtype") or "unknown error")[-800:]
            elif not isinstance(last_result.get("result"), str) or not last_result["result"].strip():
                output["error"] = "Claude returned no result text"
            else:
                output["status"] = "SUCCEEDED"
                output["result"] = last_result["result"][:80000]
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
