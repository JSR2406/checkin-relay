"""MCP client smoke test for the Check-in Relay server.

Run the server first::

    python mcp_server.py

then in a second terminal::

    python test_client.py

The test verifies the negotiated protocol version, that all four required tools
are registered, and that each one answers over Streamable HTTP.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp.client import Client
from mcp_types.version import is_version_at_least

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")

# The hackathon requires the primary MCP route to speak 2025-11-25 or later.
REQUIRED_VERSION = "2025-11-25"
REQUIRED_TOOLS = {"log_checkin", "get_status", "schedule_checkin", "escalate"}

PASS = "PASS"
FAIL = "FAIL"


def _render(value: Any) -> str:
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), indent=2, default=str)
    return json.dumps(value, indent=2, default=str)


async def _call(client: Client, name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool and fail loudly if the server reported an error."""
    result = await client.call_tool(name, arguments)
    if result.is_error:
        text = "".join(block.text for block in result.content if hasattr(block, "text"))
        raise RuntimeError(f"tool {name!r} failed: {text}")
    return result.structured_content


async def main() -> int:
    failures = 0

    print(f"Connecting to {MCP_URL} over Streamable HTTP ...")
    async with Client(MCP_URL, mode="auto", raise_exceptions=True) as client:
        version = client.protocol_version
        print(f"Negotiated protocol version: {version}")
        print(f"Server info: {client.server_info}")

        if version and is_version_at_least(version, REQUIRED_VERSION):
            print(f"{PASS} protocol version {version} >= {REQUIRED_VERSION}")
        else:
            print(f"{FAIL} protocol version {version} is older than {REQUIRED_VERSION}")
            failures += 1

        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        print(f"\nTools advertised: {sorted(tool_names)}")
        for tool in tools.tools:
            schema = tool.input_schema or {}
            props = list((schema.get("properties") or {}).keys())
            print(f"  - {tool.name}: {tool.description}")
            print(f"    arguments: {props}")

        missing = REQUIRED_TOOLS - tool_names
        if missing:
            print(f"{FAIL} missing tools: {sorted(missing)}")
            failures += 1
        else:
            print(f"{PASS} all {len(REQUIRED_TOOLS)} required tools are registered")

        print("\n--- calling each tool ---")

        data = await _call(
            client,
            "schedule_checkin",
            {"person": "Elena Whitfield", "interval_minutes": 60, "grace_seconds": 20},
        )
        print(f"\nschedule_checkin ->\n{_render(data)}")

        data = await _call(
            client,
            "log_checkin",
            {
                "person": "Elena Whitfield",
                "status": "ok",
                "message": "Feeling good today.",
                "mood": "good",
                "source": "voice",
            },
        )
        print(f"\nlog_checkin ->\n{_render(data)}")

        data = await _call(client, "get_status", {"person": "Elena Whitfield"})
        print(f"\nget_status ->\n{_render(data)}")

        data = await _call(
            client,
            "escalate",
            {"person": "Samuel Whitfield", "reason": "Smoke test - please ignore"},
        )
        print(f"\nescalate ->\n{_render(data)}")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
