"""Static safety checks for the Caddy authentication boundary."""

from pathlib import Path


CADDYFILE = Path(__file__).parents[1] / "gateway" / "Caddyfile"


def test_gateway_health_is_public_but_mcp_has_401_fallback():
    config = CADDYFILE.read_text(encoding="utf-8")

    assert "@health path /health" in config
    assert 'respond `{"status":"ok","service":"japan-data-mcp-gateway"}` 200' in config
    assert "@mcp path /mcp /mcp/*" in config
    assert 'respond `{"error":"unauthorized"}` 401' in config
    assert "reverse_proxy {$UPSTREAM_URL}" in config


def test_gateway_supports_generic_client_token_slots():
    config = CADDYFILE.read_text(encoding="utf-8")
    for variable in (
        "MCP_AUTH_TOKEN",
        "MCP_AUTH_TOKEN_CLIENT_1",
        "MCP_AUTH_TOKEN_CLIENT_2",
    ):
        assert "{$" + variable + "}" in config
