"""Run the official Pi CLI in JSON event mode; do not implement a model loop."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from claude_worker import terminate_cli_group


def cli_args(record):
    opts = record["options"]
    args = [record["executable"], "--mode", "json",
            "--provider", opts["provider"], "--model", opts["model"],
            "--session-id", record["session_id"]]
    if opts.get("tools"):
        args += ["--tools", ",".join(opts["tools"])]
    return args + ["--", record["prompt"]]


def public_events(event):
    kind = event.get("type")
    if kind in {"agent_start", "agent_settled"}:
        return [{"kind": kind}]
    if kind == "tool_execution_start":
        return [{"kind": "tool_call", "name": str(event.get("toolName", ""))[:100],
                 "tool_id": str(event.get("toolCallId", ""))[:128]}]
    if kind == "tool_execution_end":
        return [{"kind": "tool_result", "tool_id": str(event.get("toolCallId", ""))[:128],
                 "success": not bool(event.get("isError"))}]
    if kind == "message_end":
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return []
        text = "".join(str(piece.get("text", "")) for piece in message.get("content", [])
                       if isinstance(piece, dict) and piece.get("type") == "text")
        return [{"kind": "assistant_text", "text": text[:4000]}] if text else []
    return []


def main(path):
    record_path = Path(path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    event_path = record_path.with_suffix(".events.jsonl")
    receipt_path = record_path.with_suffix(".result.json")
    result = {"status": "FAILED", "finished_at": time.time()}
    seq = 0
    if event_path.exists():
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                seq = max(seq, int(json.loads(line).get("seq", 0)))
            except (TypeError, ValueError):
                pass
    timeout = (record.get("options") or {}).get("timeout_seconds", 900)
    proc = None
    settled = False
    failed = False
    assistant_text = ""
    last_error = ""
    timed_out = threading.Event()
    done = threading.Event()
    old_sigterm = None
    try:
        with event_path.open("a", encoding="utf-8") as stream:
            os.chmod(event_path, 0o600)
            def emit(value):
                nonlocal seq
                seq += 1
                stream.write(json.dumps({"seq": seq, **value}, ensure_ascii=False) + "\n")
                stream.flush()

            proc = subprocess.Popen(cli_args(record), cwd=record["cwd"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1,
                                    start_new_session=True)
            if threading.current_thread() is threading.main_thread():
                old_sigterm = signal.getsignal(signal.SIGTERM)
                def stop(_sig, _frame):
                    result.update({"status": "CANCELLED", "error": "Pi job cancelled"})
                    terminate_cli_group(proc)
                    raise SystemExit(143)
                signal.signal(signal.SIGTERM, stop)

            stderr_chunks = []
            def drain_stderr():
                n = 0
                for line in proc.stderr:
                    if n < 2000:
                        part = line[:2000 - n]
                        stderr_chunks.append(part)
                        n += len(part)
            threading.Thread(target=drain_stderr, daemon=True).start()

            deadline = [time.monotonic() + timeout]
            def monitor():
                while not done.wait(0.25):
                    if time.monotonic() >= deadline[0] and proc.poll() is None:
                        timed_out.set()
                        terminate_cli_group(proc)
                        return
            threading.Thread(target=monitor, daemon=True).start()
            for line in proc.stdout:
                if len(line) > 2_000_000:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                deadline[0] = time.monotonic() + timeout
                if event.get("type") == "agent_settled":
                    settled = True
                if event.get("type") == "message_end":
                    msg = event.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        if msg.get("stopReason") in ("error", "aborted"):
                            failed = True
                            last_error = str(msg.get("errorMessage", ""))[:1000]
                        assistant_text = "".join(
                            str(c.get("text", "")) for c in msg.get("content", [])
                            if isinstance(c, dict) and c.get("type") == "text"
                        )[-80000:]
                for public in public_events(event):
                    emit(public)
            exitcode = proc.wait()
            done.set()
            if timed_out.is_set():
                result.update({"status": "STALLED", "error": "Pi task idle timeout"})
            elif exitcode != 0 or failed or not settled:
                result["error"] = last_error or "".join(stderr_chunks)[-1200:] or "Pi did not settle successfully"
            else:
                result.update({"status": "SUCCEEDED", "result": assistant_text})
    except (OSError, ValueError, KeyError) as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        done.set()
        if proc is not None and proc.poll() is None:
            terminate_cli_group(proc)
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        if old_sigterm is not None:
            signal.signal(signal.SIGTERM, old_sigterm)
        result["finished_at"] = time.time()
        temp = receipt_path.with_name(receipt_path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            os.chmod(temp, 0o600)
            json.dump(result, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, receipt_path)
        with event_path.open("a", encoding="utf-8") as handle:
            os.chmod(event_path, 0o600)
            handle.write(json.dumps({"seq": seq + 1, "kind": "completion",
                                     "status": result["status"],
                                     "success": result["status"] == "SUCCEEDED"}) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    main(sys.argv[1])
