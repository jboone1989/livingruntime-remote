"""Read-only or workspace-scoped native Codex CLI runner.

Consume official `codex exec --json` events; no custom model API or agent loop.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from claude_worker import terminate_cli_group


def cli_args(record):
    opts = record.get("options") or {}
    exe = record["executable"]
    # Codex exec owns its sandbox. Never pass bypass/full-access switches.
    if record.get("continue"):
        thread_path = Path(record["path"]).with_suffix(".thread")
        thread_id = thread_path.read_text(encoding="utf-8").strip()
        if str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError("invalid native Codex thread id")
        # Noninteractive resume uses the persistent native Codex conversation.
        return [exe, "exec", "resume", "--json", thread_id, record["prompt"]]
    args = [exe, "exec", "--json", "--cd", record["cwd"],
            "--sandbox", opts.get("sandbox", "read-only")]
    if opts.get("model"):
        args += ["--model", opts["model"]]
    if opts.get("profile"):
        args += ["--profile", opts["profile"]]
    return args + ["--", record["prompt"]]


def public_events(raw):
    kind = raw.get("type")
    if kind == "thread.started":
        return [{"kind": "session", "thread_id": str(raw.get("thread_id", ""))[:128]}]
    if kind == "turn.started":
        return [{"kind": "turn_started"}]
    if kind == "turn.completed":
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return [{"kind": "turn_result", "success": True,
                 "usage": {k: v for k, v in usage.items()
                           if k in ("input_tokens", "cached_input_tokens", "output_tokens",
                                    "reasoning_output_tokens") and type(v) is int}}]
    if kind in ("turn.failed", "error"):
        msg = raw.get("message")
        if not isinstance(msg, str):
            data = raw.get("error")
            msg = data.get("message", "") if isinstance(data, dict) else ""
        return [{"kind": "error", "message": str(msg)[:1000]}]
    if kind not in ("item.started", "item.updated", "item.completed"):
        return []
    item = raw.get("item")
    if not isinstance(item, dict):
        return []
    item_type = str(item.get("type", ""))[:100]
    item_id = str(item.get("id", ""))[:128]
    if item_type == "agent_message":
        if kind == "item.completed" and isinstance(item.get("text"), str):
            return [{"kind": "assistant_text", "text": item["text"][:4000]}]
        return []
    if item_type in ("reasoning", "plan"):
        return []
    if kind == "item.started":
        # Intentionally omit commands, file diffs, MCP arguments, tool outputs.
        return [{"kind": "tool_call", "name": item_type, "tool_id": item_id}]
    if kind == "item.completed":
        return [{"kind": "tool_result", "tool_id": item_id,
                 "success": item.get("status") not in ("failed", "error")}]
    return []


def main(path):
    record_path = Path(path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    event_path = record_path.with_suffix(".events.jsonl")
    receipt_path = record_path.with_suffix(".result.json")
    thread_path = record_path.with_suffix(".thread")
    result = {"status": "FAILED", "finished_at": time.time()}
    proc = None
    seq = 0
    if event_path.exists():
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                seq = max(seq, int(json.loads(line).get("seq", 0)))
            except (TypeError, ValueError):
                continue
    done = threading.Event()
    lock = threading.Lock()
    deadline = [time.monotonic() + (record.get("options") or {}).get("timeout_seconds", 900)]
    timed_out = [False]
    last_text = ""
    turn_failed = False
    turn_completed = False
    usage = {}
    stderr_parts = []

    try:
        args = cli_args({**record, "path": str(record_path)})
        with event_path.open("a", encoding="utf-8") as out:
            os.chmod(event_path, 0o600)

            def emit(event):
                nonlocal seq
                seq += 1
                out.write(json.dumps({"seq": seq, **event}, ensure_ascii=False) + "\n")
                out.flush()

            proc = subprocess.Popen(args, cwd=record["cwd"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1, start_new_session=True)

            def drain_stderr():
                total = 0
                for chunk in proc.stderr:
                    if total < 4000:
                        stderr_parts.append(chunk[:4000 - total])
                        total += len(stderr_parts[-1])

            threading.Thread(target=drain_stderr, daemon=True).start()

            def monitor():
                while not done.wait(0.25):
                    with lock:
                        expired = time.monotonic() >= deadline[0]
                    if expired and proc.poll() is None:
                        timed_out[0] = True
                        terminate_cli_group(proc)
                        return

            threading.Thread(target=monitor, daemon=True).start()
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
                    with lock:
                        deadline[0] = time.monotonic() + (record.get("options") or {}).get("timeout_seconds", 900)
                    if raw.get("type") == "thread.started":
                        native = raw.get("thread_id")
                        if not isinstance(native, str) or str(uuid.UUID(native)) != native:
                            raise ValueError("Codex returned invalid thread id")
                        if thread_path.exists() and thread_path.read_text(encoding="utf-8").strip() != native:
                            raise ValueError("Codex thread changed during resume")
                        if not thread_path.exists():
                            with thread_path.open("x", encoding="utf-8") as f:
                                os.chmod(thread_path, 0o600)
                                f.write(native)
                    elif raw.get("type") == "turn.completed":
                        turn_completed = True
                        if isinstance(raw.get("usage"), dict):
                            usage = raw["usage"]
                    elif raw.get("type") in ("turn.failed", "error"):
                        turn_failed = True
                        problem = raw.get("error")
                        result["error"] = str(problem.get("message", "") if isinstance(problem, dict)
                                              else raw.get("message", "Codex turn failed"))[:900]
                    elif raw.get("type") == "item.completed":
                        item = raw.get("item")
                        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                            last_text = item["text"][:80000]
                    for event in public_events(raw):
                        emit(event)
                returncode = proc.wait()
            finally:
                done.set()
                if proc.poll() is None:
                    terminate_cli_group(proc)
            if timed_out[0]:
                result.update({"status": "STALLED", "error": "Codex task idle timeout"})
            elif returncode:
                result["error"] = "Codex exit=%s: %s" % (returncode, "".join(stderr_parts)[-1200:])
            elif turn_failed or not turn_completed:
                result.setdefault("error", "Codex did not finish a successful turn")
            else:
                result.update({"status": "SUCCEEDED", "result": last_text,
                               "usage": usage})
    except (OSError, ValueError, KeyError) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:500]
    finally:
        done.set()
        result["finished_at"] = time.time()
        tmp = receipt_path.with_name(receipt_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            json.dump(result, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, receipt_path)
        with event_path.open("a", encoding="utf-8") as f:
            os.chmod(event_path, 0o600)
            f.write(json.dumps({"seq": seq + 1, "kind": "completion",
                                "status": result["status"], "success": result["status"] == "SUCCEEDED"},
                               ensure_ascii=False) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    main(sys.argv[1])
