"""Trusted owner-main Connector consumes Relay Pi patch inbox with GitHub gh.

The Relay has no GitHub credentials. The cn-dev Pi has no credentials either.
Exactly one non-force commit is written by the trusted, version-pinned writer.
"""
from __future__ import annotations

from eventgraph_pi_patch_writer import write


def drain(cfg, request, writer=write):
    if cfg.get("name") != "owner-main":
        return False
    response = request(cfg["url"], "/device/pi-repair-inbox", {"action": "pending"},
                       cfg["device_token"], timeout=30)
    items = response.get("items")
    if not isinstance(items, list) or len(items) > 5:
        raise ValueError("invalid bounded repair inbox")
    delivered = False
    for item in items:
        try:
            if not isinstance(item, dict) or set(item) != {
                "session_id", "pr_number", "original_sha", "manifest"
            }:
                raise ValueError("invalid pending repair envelope")
            manifest = item["manifest"]
            if (manifest["pr_number"] != item["pr_number"] or
                manifest["expected_head_sha"] != item["original_sha"]):
                raise PermissionError("pending repair identity mismatch")
            receipt = writer(manifest)
            if (receipt["pr_number"] != item["pr_number"] or
                receipt["previous_sha"] != item["original_sha"]):
                raise PermissionError("writer returned the wrong repair identity")
            result = request(cfg["url"], "/device/pi-repair-inbox",
                             {"action": "ack", "session_id": item["session_id"],
                              "original_sha": item["original_sha"], "new_sha": receipt["new_sha"]},
                             cfg["device_token"], timeout=30)
            if result.get("ok") is not True:
                raise RuntimeError("Relay did not acknowledge pushed patch")
            delivered = True
        except Exception as exc:
            # Leave the durable inbox item unacknowledged. On retry, the trusted
            # writer reconciles exact commit contents and does not push again.
            print("Pi repair GitHub write deferred: " + type(exc).__name__, flush=True)
    return delivered
