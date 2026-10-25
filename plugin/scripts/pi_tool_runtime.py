"""Execute native Pi SDK tools on the Connector host without invoking a model.

The Node gateway is intentionally small; Remote still owns project and command
authorization. Bash is routed through Remote's existing exec permissions.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).with_name("pi_tool.mjs")
_ALLOWED = frozenset({"read", "edit", "write", "grep", "find", "ls"})


def execute(tool: str, params: dict, *, cwd: str, timeout_seconds: int = 30) -> dict:
    if tool not in _ALLOWED:
        raise ValueError("unsupported Pi tool (bash uses Remote exec authorization)")
    if not isinstance(params, dict):
        raise ValueError("Pi params must be an object")
    timeout = max(1, min(30, int(timeout_seconds)))
    workspace = Path(cwd).resolve(strict=True)
    sidecar = workspace / ".livingruntime" / "pi-harness" / "pi_tool.mjs"
    gateway = sidecar if sidecar.is_file() else _SCRIPT
    process = subprocess.run(
        ["node", str(gateway)],
        cwd=str(workspace),
        input=json.dumps({"tool": tool, "params": params}),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    if len(process.stdout) > 200_000:
        raise RuntimeError("Pi SDK result exceeds output limit")
    try:
        result = json.loads(process.stdout)
    except ValueError:
        raise RuntimeError("Pi SDK unavailable; install plugin Node dependencies") from None
    if not isinstance(result, dict):
        raise RuntimeError("Pi SDK returned an invalid result")
    if process.returncode and result.get("ok"):
        result["ok"] = False
    return result
