# Sliplane deployment

This fork supports a two-service deployment:

- `japan-data-mcp-core`: private Streamable HTTP MCP server on port 8000
- `japan-data-mcp-gateway`: public Caddy gateway on port 8080

The gateway exposes `/health` without authentication and requires a Bearer
token for `/mcp`. Keep the core service private and configure the gateway's
`UPSTREAM_URL` with the core service's Sliplane internal endpoint.

## Core environment variables

- `ESTAT_APP_ID` (required)
- `CORP_APP_ID` (optional; also enables invoice tools)
- `REALESTATE_API_KEY` (optional)
- `MCP_TRANSPORT=streamable-http`
- `MCP_HOST=0.0.0.0`
- `PORT=8000`

## Gateway environment variables

- `UPSTREAM_URL`
- `MCP_AUTH_TOKEN`
- `MCP_AUTH_TOKEN_CODEX`
- `MCP_AUTH_TOKEN_NOTION`

Store every real credential in Sliplane environment variables. Do not create
or commit a production `.env` file.
