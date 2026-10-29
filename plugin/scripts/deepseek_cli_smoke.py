"""One-turn, redacted DeepSeek-Flash smoke test using the native Claude CLI."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

from claude_worker import provider_environment

MARKER = "DEEPSEEK_FLASH_REMOTE_OK"


def main():
    argv = [
        "/home/ubuntu/.local/bin/claude", "-p",
        "Reply with exactly " + MARKER + " and nothing else.",
        "--output-format", "json", "--max-turns", "1",
        "--setting-sources", "",
    ]
    env = provider_environment({"options": {"provider_profile": "deepseek-flash"}})
    try:
        result = subprocess.run(
            argv, env=env, cwd=str(Path.cwd()),
            text=True, capture_output=True, timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("deepseek_cli_timeout")
        return 1
    if result.returncode:
        print("deepseek_cli_failed_exit=" + str(result.returncode))
        return 1
    try:
        reply = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("deepseek_cli_invalid_json")
        return 1
    if reply.get("is_error") or MARKER not in str(reply.get("result", "")):
        print("deepseek_cli_unexpected_response")
        return 1
    print("deepseek_cli_smoke_passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
