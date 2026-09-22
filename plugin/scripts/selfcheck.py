from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from contract import PLUGIN_VERSION, REMOTE_IDENTITY, REMOTE_TOOLS  # noqa: E402


def main() -> None:
    python_files = [
        ROOT / "scripts" / "bridge.py",
        ROOT / "scripts" / "launcher.py",
        ROOT / "scripts" / "configure.py",
        ROOT / "scripts" / "configmodel.py",
        ROOT / "scripts" / "install-personal.py",
        ROOT / "scripts" / "tunnel.py",
        ROOT / "scripts" / "companion.py",
        ROOT / "scripts" / "install-host.py",
        ROOT / "scripts" / "connector.py",
        ROOT / "scripts" / "relay_agent.py",
        ROOT / "scripts" / "contract.py",
    ]
    for path in python_files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
    http = json.loads((ROOT / "mcp.http.json").read_text(encoding="utf-8"))
    legacy = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    legacy_mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    apps = json.loads((ROOT / ".app.json").read_text(encoding="utf-8"))
    submission = json.loads((ROOT / "chatgpt-app-submission.json").read_text(encoding="utf-8"))

    assert plugin["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert plugin["name"] == "livingruntime-remote"
    assert plugin["version"] == PLUGIN_VERSION == "0.4.6"
    assert legacy["version"] == PLUGIN_VERSION
    assert REMOTE_IDENTITY == "livingruntime.remote"
    assert plugin["extensions"]["com.openai"]["apps"] == "./.app.json"
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert mcp["mcpServers"]["livingruntime_remote"]["type"] == "stdio"
    assert mcp["mcpServers"]["livingruntime_remote"]["command"] == "python"
    assert http["mcpServers"]["livingruntime_remote_http"]["url"].startswith("http://127.0.0.1:")
    assert legacy["mcpServers"] == "./.mcp.json"
    assert legacy["apps"] == "./.app.json"
    assert legacy_mcp["mcpServers"]["livingruntime_remote"]["type"] == "stdio"
    assert apps == {"apps": {}}
    bridge_text = (ROOT / "scripts" / "bridge.py").read_text(encoding="utf-8")
    for name in REMOTE_TOOLS:
        assert name in submission["tools"], name
        assert f'name="{name}"' in bridge_text or f"name = \"{name}\"" in bridge_text or f'def {name}(' in bridge_text, name

    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix not in {".png", ".pyc"}
        and "__pycache__" not in path.parts
    )
    forbidden = ["BEGIN PRIVATE " + "KEY", "BEGIN OPENSSH PRIVATE " + "KEY", "password" + "="]
    for marker in forbidden:
        assert marker not in combined, f"secret-like marker found: {marker}"

    print("LivingRuntime Remote plugin self-check OK")


if __name__ == "__main__":
    main()
