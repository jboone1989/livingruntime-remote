from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from cognition import complete, get, get_status, submit, wait_response


def _stdin_object() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("JSON request body required on stdin")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("stdin JSON must be an object")
    return value


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def submit_or_call(
    payload: dict[str, Any],
    *,
    wait: bool,
    wait_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    request = submit(**payload)
    if not wait:
        return request
    return wait_response(
        str(request["request_id"]),
        timeout_seconds=wait_timeout_seconds,
    )


def complete_request(
    *,
    request_id: str,
    claim_token: str,
    body: dict[str, Any],
    provider: str,
    model: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    return complete(
        request_id=request_id,
        response_text=body.get("response_text"),
        tool_calls=body.get("tool_calls"),
        claim_token=claim_token,
        provider=provider,
        model=model,
        session_id=session_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="LivingRuntime durable cognition queue")
    sub = parser.add_subparsers(dest="command", required=True)

    submit_p = sub.add_parser("submit")
    submit_p.add_argument("--wait", action="store_true")
    submit_p.add_argument("--wait-timeout-seconds", type=int, default=None)

    call_p = sub.add_parser("call")
    call_p.add_argument("--wait-timeout-seconds", type=int, default=None)

    wait_p = sub.add_parser("wait")
    wait_p.add_argument("request_id")
    wait_p.add_argument("--wait-timeout-seconds", type=int, default=None)

    status_p = sub.add_parser("status")
    status_p.add_argument("request_id")

    get_p = sub.add_parser("get")
    get_p.add_argument("request_id")

    complete_p = sub.add_parser("complete")
    complete_p.add_argument("request_id")
    complete_p.add_argument("--claim-token", required=True)
    complete_p.add_argument("--provider", default="livingruntime-chatgpt")
    complete_p.add_argument("--model", default=None)
    complete_p.add_argument("--session-id", default=None)

    args = parser.parse_args()
    if args.command in {"submit", "call"}:
        payload = _stdin_object()
        _print(submit_or_call(
            payload,
            wait=args.command == "call" or bool(getattr(args, "wait", False)),
            wait_timeout_seconds=getattr(args, "wait_timeout_seconds", None),
        ))
        return 0
    if args.command == "wait":
        _print(wait_response(
            args.request_id,
            timeout_seconds=args.wait_timeout_seconds,
        ))
        return 0
    if args.command == "status":
        _print(get_status(args.request_id))
        return 0
    if args.command == "get":
        _print(get(args.request_id))
        return 0
    if args.command == "complete":
        _print(complete_request(
            request_id=args.request_id,
            claim_token=args.claim_token,
            body=_stdin_object(),
            provider=args.provider,
            model=args.model,
            session_id=args.session_id,
        ))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise
