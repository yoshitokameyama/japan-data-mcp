# Container deployment

This fork supports a two-service deployment:

- `japan-data-mcp-core`: private Streamable HTTP MCP server on port 8000
- `japan-data-mcp-gateway`: public Caddy gateway on port 8080

The gateway exposes `/health` without authentication and requires a Bearer
token for `/mcp`. Keep the core service private and configure the gateway's
`UPSTREAM_URL` with the container platform's internal endpoint.

## Core environment variables

- `ESTAT_APP_ID` (required)
- `CORP_APP_ID` (optional; also enables invoice tools)
- `REALESTATE_API_KEY` (optional)
- `LISTINGS_JSON_URL` / `LISTINGS_API_TOKEN` (optional; licensed normalized feed)
- `VACANT_CANDIDATES_URL` / `VACANT_CANDIDATES_API_TOKEN` (optional; spatial ETL)
- `MCP_TOOL_PROFILE=curated` (recommended for ordinary AI clients; 12 tools)
- `MCP_TRANSPORT=streamable-http`
- `MCP_HOST=0.0.0.0`
- `PORT=8000`

## Gateway environment variables

- `UPSTREAM_URL`
- `MCP_AUTH_TOKEN`
- `MCP_AUTH_TOKEN_CLIENT_1`
- `MCP_AUTH_TOKEN_CLIENT_2`

Store every real credential in the deployment platform's secret store. Do not create
or commit a production `.env` file.

Use `MCP_TOOL_PROFILE=full` only for migration/admin clients that require the
legacy and advanced tools. The curated profile reduces overlapping choices for
AI clients without deleting the implementations.
