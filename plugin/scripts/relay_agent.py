from __future__ import annotations

import argparse
import json
import os
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from contract import PLUGIN_VERSION, REMOTE_TOOLS

TOOLS = set(REMOTE_TOOLS)


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
        import bridge

        fn = getattr(bridge, tool)
        result = fn(**dict(task.get("args") or {}))
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}


def serve(config_path: Path, once: bool = False) -> None:
    cfg = _load(config_path)
    while True:
        try:
            response = _request(cfg["url"], "/device/poll?wait=20", {},
                                cfg["device_token"], timeout=30)
            task = response.get("task")
            if task:
                result = _dispatch(task)
                _request(cfg["url"], "/device/result",
                         {"task_id": task["task_id"], "result": result},
                         cfg["device_token"], timeout=30)
                if once:
                    return
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
