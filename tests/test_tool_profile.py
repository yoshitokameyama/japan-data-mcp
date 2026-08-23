"""Verify that ordinary AI clients receive the curated tool catalog."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_curated_profile_exposes_thirteen_non_overlapping_tools():
    environment = dict(os.environ)
    environment["MCP_TOOL_PROFILE"] = "curated"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from japan_data_mcp.server import mcp; "
                "print(json.dumps(sorted(mcp._tool_manager._tools)))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    tools = json.loads(result.stdout)

    assert len(tools) == 13
    assert "research_real_estate_area" in tools
    assert "get_mlit_geospatial_layers" in tools
    assert "get_connector_status" in tools
    assert "search_government_open_data" in tools
    assert "get_real_estate_transactions" not in tools
    assert "search_real_estate_transactions" not in tools
