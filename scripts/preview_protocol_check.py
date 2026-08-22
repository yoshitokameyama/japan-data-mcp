"""Check a running local Streamable HTTP core without calling paid APIs."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    base_url = os.environ.get("PREVIEW_BASE_URL", "http://127.0.0.1:8123")
    auth_token = os.environ.get("PREVIEW_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else None
    async with httpx.AsyncClient(timeout=10, headers=headers) as http:
        health = await http.get(f"{base_url}/health")
        health.raise_for_status()

        async with streamable_http_client(
            f"{base_url}/mcp", http_client=http
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                status = await session.call_tool("get_connector_status", {})
                sample = await session.call_tool(
                    "research_real_estate_area",
                    {
                        "area_name": "谷中",
                        "city_code": "13106",
                        "target_area_sqm": 100,
                    },
                )

    status_text = "".join(
        item.text for item in status.content if getattr(item, "type", None) == "text"
    )
    status_payload = json.loads(status_text)
    sample_text = "".join(
        item.text for item in sample.content if getattr(item, "type", None) == "text"
    )
    sample_payload = json.loads(sample_text)
    print(
        json.dumps(
            {
                "health_status": health.status_code,
                "health_body": health.json(),
                "tool_count": len(tools.tools),
                "representative_tools_present": all(
                    name in {tool.name for tool in tools.tools}
                    for name in (
                        "research_real_estate_area",
                        "get_mlit_geospatial_layers",
                        "get_connector_status",
                    )
                ),
                "connector_status": status_payload["status"],
                "read_only": status_payload["data"]["read_only"],
                "sample_tool_status": sample_payload["status"],
                "sample_layer_statuses": {
                    "transactions": sample_payload["data"]["transactions"]["status"],
                    "current_listings": sample_payload["data"]["current_listings"]["status"],
                    "vacant_candidates": sample_payload["data"]["vacant_candidates"]["status"],
                },
                "sample_layers_separated": all(
                    key in sample_payload["data"]
                    for key in (
                        "transactions",
                        "current_listings",
                        "vacant_candidates",
                    )
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
