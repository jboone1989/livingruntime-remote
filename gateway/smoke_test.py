from __future__ import annotations

import argparse
import asyncio
import os

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def _main(url: str) -> None:
    token = os.environ.get("MCP_GATEWAY_TOKEN", "")
    if not token:
        raise SystemExit("MCP_GATEWAY_TOKEN is required")
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(10.0, read=30.0),
    ) as http_client:
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport) as client:
            tools = await client.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            expected = {
                "connection_status",
                "gateway_status",
                "exec",
                "read_file",
                "write_file",
                "list_dir",
                "git",
                "process",
                "systemd",
                "logs",
            }
            missing = sorted(expected - set(names))
            if missing:
                raise RuntimeError(f"missing tools: {missing}")
            result = await client.call_tool("connection_status", {})
            if result.is_error:
                raise RuntimeError(f"connection_status failed: {result.content}")
            print("MCP smoke test OK")
            print("tools:", ", ".join(names))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()
    asyncio.run(_main(args.url))
