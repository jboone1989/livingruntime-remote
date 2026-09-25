#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from typing import Any

from cognition import submit, wait_response

MAX_STDIN_BYTES = 512 * 1024


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise ValueError("cognition request exceeds maximum transport size")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("cognition request must be a JSON object")
    return value


def submit_wait(payload: dict[str, Any]) -> dict[str, Any]:
    request = submit(
        agent_id=payload.get("agent_id"),
        purpose=payload.get("purpose"),
        messages=payload.get("messages"),
        tools=payload.get("tools"),
        response_format=payload.get("response_format"),
        options=payload.get("options"),
        metadata=payload.get("metadata"),
        timeout_seconds=payload.get("timeout_seconds", 300),
        request_id=payload.get("request_id"),
    )
    return wait_response(
        str(request["request_id"]),
        timeout_seconds=int(payload.get("timeout_seconds", 300)),
    )


def main(argv: list[str]) -> int:
    if argv != ["submit-wait"]:
        raise ValueError("usage: cognition_cli.py submit-wait")
    result = submit_wait(_read_payload())
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        message = str(exc).replace("\n", " ")[:2000]
        sys.stderr.write(f"{type(exc).__name__}: {message}\n")
        raise SystemExit(1)
