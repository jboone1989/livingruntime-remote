from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("LIVINGRUNTIME_RELAY_ISSUER", "https://auth.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_AUDIENCE", "https://remote.example.test")
os.environ.setdefault("LIVINGRUNTIME_RELAY_JWKS_URL", "https://auth.example.test/jwks.json")
os.environ.setdefault("LIVINGRUNTIME_RELAY_RESOURCE_URL", "https://remote.example.test/mcp")
os.environ.setdefault("LIVINGRUNTIME_RELAY_DB", str(Path(tempfile.gettempdir()) / "lr-device-test.sqlite3"))

import server


class FakeRelay:
    def __init__(self):
        self.calls = []
        self.records = [
            {"device_id": "aaa", "name": "owner-main", "online": True, "default": True},
            {"device_id": "bbb", "name": "cn-dev", "online": True, "default": False},
            {"device_id": "ccc", "name": "old-offline", "online": False, "default": False},
        ]

    def connector_statuses(self, user_sub):
        assert user_sub == "owner-a"
        return self.records

    async def call(self, user_sub, tool, arguments, *, connector=None):
        self.calls.append((user_sub, tool, arguments, connector))
        if connector == "aaa":
            return {"devices": [{"device_id": "main", "name": "VM09", "default": True, "online": True}]}
        if connector == "bbb":
            return {"devices": [{"device_id": "main", "name": "cn-dev", "default": True, "online": True}]}
        raise AssertionError("offline Connector should not be called")


class AggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_inner_device_name_is_not_collapsed(self):
        relay = FakeRelay()
        result = await server.aggregate_connector_devices(relay, "owner-a", True)
        self.assertEqual([r["device_id"] for r in result["devices"]], ["owner-main::main", "cn-dev::main"])
        self.assertEqual(result["default_device"], "owner-main::main")
        self.assertEqual([r["default"] for r in result["devices"]], [True, False])
        self.assertEqual(len(result["connectors"]), 3)
        self.assertEqual(result["errors"], [])
        self.assertEqual({c[3] for c in relay.calls}, {"aaa", "bbb"})
        self.assertTrue(all(c[2]["include_resources"] for c in relay.calls))

    async def test_failed_connector_does_not_hide_other_hosts(self):
        relay = FakeRelay()
        original = relay.call

        async def partial_failure(user_sub, tool, arguments, *, connector=None):
            if connector == "bbb":
                raise RuntimeError("connector disconnected")
            return await original(user_sub, tool, arguments, connector=connector)

        relay.call = partial_failure
        result = await server.aggregate_connector_devices(relay, "owner-a")
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(result["errors"], [{"connector": "cn-dev", "error": "connector disconnected"}])

    async def test_no_online_connectors(self):
        relay = FakeRelay()
        relay.records = [{**r, "online": False} for r in relay.records]
        result = await server.aggregate_connector_devices(relay, "owner-a")
        self.assertEqual(result["devices"], [])
        self.assertIsNone(result["default_device"])
        self.assertFalse(relay.calls)
        self.assertEqual(len(result["connectors"]), 3)


if __name__ == "__main__":
    unittest.main()
