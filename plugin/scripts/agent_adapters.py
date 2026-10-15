"""Agent adapter contracts; safe to import with zero installed agents."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, Any
import importlib
import json
import os
from pathlib import Path

@dataclass(frozen=True)
class AgentCapabilities:
    resume: bool = False
    messaging: bool = False
    events: bool = False
    cancel: bool = False

class AgentAdapter(Protocol):
    adapter_id: str
    def capabilities(self) -> AgentCapabilities: ...
    def start(self, goal: str, *, project: str, device: str) -> str: ...
    def status(self, session_id: str) -> dict[str, Any]: ...
    def send(self, session_id: str, message: str) -> None: ...
    def resume(self, session_id: str) -> dict[str, Any]: ...
    def cancel(self, session_id: str) -> None: ...
    def events(self, session_id: str, *, after: int = 0) -> list[dict[str, Any]]: ...

class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        name = getattr(adapter, "adapter_id", "")
        if not isinstance(name, str) or not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid adapter id")
        if name in self._adapters:
            raise ValueError("duplicate adapter id")
        if not isinstance(adapter.capabilities(), AgentCapabilities):
            raise TypeError("invalid capabilities")
        self._adapters[name] = adapter

    def list_agents(self) -> list[dict[str, Any]]:
        return [{"id": key, "capabilities": vars(adapter.capabilities())}
                for key, adapter in sorted(self._adapters.items())]

    def get(self, adapter_id: str) -> AgentAdapter:
        if adapter_id not in self._adapters:
            raise KeyError("agent adapter is not installed")
        return self._adapters[adapter_id]

registry = AdapterRegistry()

def load_installed(registry_obj: AdapterRegistry | None = None) -> list[str]:
    """Load only operator-listed Python entry points; no directory scanning."""
    target = registry_obj if registry_obj is not None else registry
    path = Path(os.environ.get("LIVINGRUNTIME_AGENT_ADAPTERS", str(Path.home()/".livingruntime"/"agent-adapters.json")))
    if not path.exists():
        return []
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("adapters"), list):
        raise ValueError("invalid adapter manifest")
    loaded = []
    for row in config["adapters"]:
        if not isinstance(row, dict) or set(row) != {"module","factory"}:
            raise ValueError("invalid adapter entry")
        module, factory = row["module"], row["factory"]
        if not isinstance(module,str) or not module.startswith("livingruntime_adapter_") or not module.replace("_","").isalnum():
            raise ValueError("adapter module must use explicit trusted namespace")
        if not isinstance(factory,str) or not factory.isidentifier():
            raise ValueError("invalid adapter factory")
        adapter = getattr(importlib.import_module(module), factory)()
        target.register(adapter)
        loaded.append(adapter.adapter_id)
    return loaded
