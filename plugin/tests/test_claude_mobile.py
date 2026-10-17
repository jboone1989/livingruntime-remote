"""Tests for the mobile Claude entrypoint (no live Claude login required)."""
import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import claude_mobile


def args(action, **kwargs):
    values = {"action": action, "project": "livingruntime-remote",
              "device": "main", "message": None, "session": None, "wait": 0}
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_ask_uses_existing_allowlisted_adapter(monkeypatch):
    monkeypatch.setattr(claude_mobile.agent_policy, "may_launch", lambda *a: True)
    monkeypatch.setattr(claude_mobile.agent_policy, "claude_workspace", lambda *a: "/tmp")
    invoked = []

    def start(adapter, message, *, project, device, authorized):
        invoked.append((adapter, message, project, device, authorized))
        return {"session_id": "lra_abc", "adapter_id": adapter}

    monkeypatch.setattr(claude_mobile.agent_sessions, "start", start)
    monkeypatch.setattr(claude_mobile.agent_sessions, "status",
                        lambda sid: {"session_id": sid, "agent_status": {
                            "status": "SUCCEEDED", "result": "CONNECTOR_OK"}})
    result = claude_mobile.run(args("ask", message="Reply CONNECTOR_OK"))
    assert result["agent_status"]["result"] == "CONNECTOR_OK"
    assert invoked == [("claude-code", "Reply CONNECTOR_OK",
                        "livingruntime-remote", "main", True)]


def test_launch_refused_outside_explicit_allowlist(monkeypatch):
    monkeypatch.setattr(claude_mobile.agent_policy, "may_launch", lambda *a: False)
    with pytest.raises(PermissionError):
        claude_mobile.run(args("start", message="test"))


def test_resume_and_cancel_are_session_based(monkeypatch):
    monkeypatch.setattr(claude_mobile.agent_sessions, "send",
                        lambda sid, msg: {"accepted": True, "session_id": sid})
    monkeypatch.setattr(claude_mobile.agent_sessions, "status",
                        lambda sid: {"session_id": sid, "agent_status": {
                            "status": "RUNNING"}})
    monkeypatch.setattr(claude_mobile.agent_sessions, "cancel",
                        lambda sid: {"session_id": sid, "cancel_requested": True})
    assert claude_mobile.run(args("send", session="lra_abc", message="continue"))["accepted"]
    assert claude_mobile.run(args("read", session="lra_abc"))["agent_status"]["status"] == "RUNNING"
    assert claude_mobile.run(args("stop", session="lra_abc"))["cancel_requested"]
