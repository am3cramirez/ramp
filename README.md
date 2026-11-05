# ramp-mcp

An [MCP](https://modelcontextprotocol.io) server for the
[Ramp Developer API](https://docs.ramp.com/developer-api/v1/introduction), built
to run inside [Hermes Agent](https://hermes-agent.nousresearch.com) (or any
other MCP client).

It authenticates with your Ramp app's **client credentials** (OAuth 2.0), caches
the access token, retries on rate limits (HTTP 429), follows Ramp's keyset
pagination, and ships Ramp's OpenAPI spec so the agent can find and call any of
the ~280 endpoints. It is **read-only by default**.

## Tools

| Tool | What it does |
| --- | --- |
| `ramp_get_business` | Business profile and balance |
| `ramp_list_transactions`, `ramp_list_bills`, `ramp_list_reimbursements`, `ramp_list_users`, `ramp_list_physical_cards`, `ramp_list_virtual_cards`, `ramp_list_funds`, `ramp_list_spend_programs`, `ramp_list_vendors`, `ramp_list_merchants`, `ramp_list_departments`, `ramp_list_locations`, `ramp_list_entities`, `ramp_list_receipts`, `ramp_list_memos`, `ramp_list_statements`, `ramp_list_payments`, `ramp_list_purchase_orders`, `ramp_list_cashbacks`, `ramp_list_transfers`, `ramp_list_trips`, `ramp_list_gl_accounts` | List a resource. Take `filters` (e.g. `{"from_date": "...", "state": "CLEARED"}`), `page_size`, `max_pages`, `next_url`. Each tool's description lists its filters. |
| `ramp_get` | Fetch one object by ID (`transaction`, `bill`, `user`, `vendor`, `virtual_card`, ...) |
| `ramp_search_endpoints` | Keyword search over every Ramp endpoint |
| `ramp_describe_endpoint` | Parameters, request body and response schema for an endpoint |
| `ramp_api_request` | Call any endpoint. Non-GET calls need `RAMP_ALLOW_WRITES=true`. |

## 1. Create a Ramp API app

1. In Ramp, go to **Settings → Developer API** (production: <https://app.ramp.com/settings/ramp-developer>;
   sandbox: <https://demo.ramp.com>).
2. Create an app, enable the **Client Credentials** grant, and turn on the scopes you want
   (e.g. `transactions:read`, `bills:read`, `users:read`, ...).
3. Copy the **Client ID** and **Client Secret**.

The server requests the scopes in `RAMP_SCOPES`. **They must be a subset of the scopes
enabled on your app**, otherwise Ramp rejects the token request with
`The requested scope is invalid...`. Set `RAMP_SCOPES` to exactly what you enabled.

## 2. Install

**Easiest:** with [uv](https://docs.astral.sh/uv/) installed, Hermes can run the server
straight from GitHub; no clone needed. Use this in step 3:

```yaml
    command: "uvx"
    args: ["--from", "git+https://github.com/am3cramirez/ramp", "ramp-mcp"]
```

**Or install from a clone** (requires Python 3.10+):

```bash
git clone https://github.com/am3cramirez/ramp.git ~/ramp-mcp
```

```bash
cd ~/ramp-mcp && python3 -m venv .venv && .venv/bin/pip install -e .
```

That gives you the command `~/ramp-mcp/.venv/bin/ramp-mcp`.
(With [uv](https://docs.astral.sh/uv/) you can instead run it with
`uv run --directory ~/ramp-mcp ramp-mcp` and skip the venv step.)

## 3. Add it to Hermes

Add this to `~/.hermes/config.yaml`, replacing the path and credentials:

```yaml
mcp_servers:
  ramp:
    command: "/home/YOU/ramp-mcp/.venv/bin/ramp-mcp"
    env:
      RAMP_CLIENT_ID: "your-client-id"
      RAMP_CLIENT_SECRET: "your-client-secret"
      RAMP_ENV: "production"          # or "sandbox" (demo-api.ramp.com)
      RAMP_SCOPES: "business:read transactions:read bills:read reimbursements:read users:read cards:read vendors:read"
      RAMP_ALLOW_WRITES: "false"      # set "true" to allow POST/PATCH/PUT/DELETE
    timeout: 120
```

Then run `/reload-mcp` in Hermes (or restart it). Hermes registers the tools
with an `mcp_ramp_` prefix. To expose only a subset, add for example:

```yaml
    tools:
      include: ["ramp_list_*", "ramp_get*", "ramp_search_endpoints", "ramp_describe_endpoint"]
```

### Running it as an HTTP server instead

```bash
RAMP_CLIENT_ID=... RAMP_CLIENT_SECRET=... ramp-mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

```yaml
mcp_servers:
  ramp:
    url: "http://127.0.0.1:8765/mcp"
```

The HTTP mode has no authentication of its own — anyone who can reach the port
can use your Ramp credentials. Keep it on localhost or put it behind an
authenticating proxy.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `RAMP_CLIENT_ID` / `RAMP_CLIENT_SECRET` | — | App credentials for the client-credentials flow (recommended) |
| `RAMP_ACCESS_TOKEN` | — | Alternatively, a pre-issued access token (used as-is, not refreshed) |
| `RAMP_ENV` | `production` | `production` (`api.ramp.com`) or `sandbox` (`demo-api.ramp.com`) |
| `RAMP_BASE_URL` | — | Override the API host |
| `RAMP_SCOPES` | a broad set of `:read` scopes | Space- or comma-separated scopes to request |
| `RAMP_ALLOW_WRITES` | `false` | Allow non-GET requests through `ramp_api_request` |
| `RAMP_ALLOW_VAULT` | `false` | Allow card vault endpoints (full card numbers / CVV) |
| `RAMP_TIMEOUT` | `60` | Per-request timeout, seconds |
| `RAMP_MAX_RETRIES` | `3` | Retries for 429s and, on reads, 502/503/504 and network errors |

## Safety notes

- Writes in production move real money and change real records. Leave
  `RAMP_ALLOW_WRITES` off unless you need it, and grant your Ramp app only the
  scopes you need — the scopes are the real permission boundary.
- Token endpoints can't be called through the tools, and paginated `next_url`s
  are only followed on the configured Ramp host.
- Ramp's limit is 200 requests per 10 seconds per IP; a 429 can block for 60s.
  Large `max_pages` values on big datasets will be slow.

## Development

```bash
pip install -e ".[dev]" && pytest
```

Refresh the bundled OpenAPI spec from docs.ramp.com:

```bash
python scripts/update_spec.py
```
