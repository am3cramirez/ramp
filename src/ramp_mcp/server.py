"""MCP server exposing the Ramp Developer API.

Tools fall into three groups:

* Curated, read-only tools for the most common resources
  (``ramp_list_transactions``, ``ramp_get`` ...).
* Discovery tools over the bundled OpenAPI spec
  (``ramp_search_endpoints``, ``ramp_describe_endpoint``).
* A generic ``ramp_api_request`` tool that can reach any endpoint. Non-GET
  requests are blocked unless ``RAMP_ALLOW_WRITES=true``; sensitive card vault
  endpoints are blocked unless ``RAMP_ALLOW_VAULT=true``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Literal

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    _SDK_V2 = True
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

    _SDK_V2 = False

from .client import RampClient, RampError
from .config import Settings
from .spec import load_spec

INSTRUCTIONS = """\
Tools for the Ramp corporate card / spend management Developer API.

Tips:
- Money amounts in Ramp responses are usually integers in the smallest currency
  unit (e.g. cents) or objects like {"amount": 12345, "currency_code": "USD"}.
  Check the field before presenting values.
- List tools return {"data": [...], "next": <url|null>}. Pass `next` back as
  `next_url` to continue, or raise `max_pages` to fetch several pages at once.
- Dates/filters are passed via the `filters` object, e.g.
  {"from_date": "2026-01-01T00:00:00Z", "to_date": "2026-02-01T00:00:00Z"}.
- For anything without a dedicated tool: ramp_search_endpoints ->
  ramp_describe_endpoint -> ramp_api_request.
- A 403 usually means the API token lacks the scope listed for the endpoint.
"""

# name -> (path, short description)
LIST_RESOURCES: dict[str, tuple[str, str]] = {
    "transactions": ("/developer/v1/transactions", "card transactions"),
    "bills": ("/developer/v1/bills", "bills (accounts payable)"),
    "reimbursements": ("/developer/v1/reimbursements", "employee reimbursements"),
    "users": ("/developer/v1/users", "users / employees"),
    "physical_cards": ("/developer/v1/cards/physical", "physical cards"),
    "virtual_cards": ("/developer/v1/cards/virtual", "virtual cards"),
    "funds": ("/developer/v1/funds", "funds (spend limits)"),
    "spend_programs": ("/developer/v1/spend-programs", "spend programs"),
    "vendors": ("/developer/v1/vendors", "vendors"),
    "merchants": ("/developer/v1/merchants", "merchants"),
    "departments": ("/developer/v1/departments", "departments"),
    "locations": ("/developer/v1/locations", "locations"),
    "entities": ("/developer/v1/entities", "business entities"),
    "receipts": ("/developer/v1/receipts", "receipts"),
    "memos": ("/developer/v1/memos", "transaction memos"),
    "statements": ("/developer/v1/statements", "card statements"),
    "payments": ("/developer/v1/payments", "vendor payments"),
    "purchase_orders": ("/developer/v1/purchase-orders", "purchase orders"),
    "cashbacks": ("/developer/v1/cashbacks", "cashback payments"),
    "transfers": ("/developer/v1/transfers", "transfer payments"),
    "trips": ("/developer/v1/trips", "trips"),
    "gl_accounts": ("/developer/v1/accounting/accounts", "general ledger accounts"),
}

GET_RESOURCES: dict[str, str] = {
    "transaction": "/developer/v1/transactions/{id}",
    "bill": "/developer/v1/bills/{id}",
    "draft_bill": "/developer/v1/bills/drafts/{id}",
    "reimbursement": "/developer/v1/reimbursements/{id}",
    "user": "/developer/v1/users/{id}",
    "physical_card": "/developer/v1/cards/physical/{id}",
    "virtual_card": "/developer/v1/cards/virtual/{id}",
    "fund": "/developer/v1/funds/{id}",
    "spend_program": "/developer/v1/spend-programs/{id}",
    "vendor": "/developer/v1/vendors/{id}",
    "department": "/developer/v1/departments/{id}",
    "location": "/developer/v1/locations/{id}",
    "entity": "/developer/v1/entities/{id}",
    "receipt": "/developer/v1/receipts/{id}",
    "memo": "/developer/v1/memos/{id}",
    "statement": "/developer/v1/statements/{id}",
    "payment": "/developer/v1/payments/{id}",
    "purchase_order": "/developer/v1/purchase-orders/{id}",
    "cashback": "/developer/v1/cashbacks/{id}",
    "transfer": "/developer/v1/transfers/{id}",
    "trip": "/developer/v1/trips/{id}",
    "gl_account": "/developer/v1/accounting/accounts/{id}",
}

GetResource = Literal[
    "transaction", "bill", "draft_bill", "reimbursement", "user", "physical_card",
    "virtual_card", "fund", "spend_program", "vendor", "department", "location",
    "entity", "receipt", "memo", "statement", "payment", "purchase_order",
    "cashback", "transfer", "trip", "gl_account",
]


def _fail(exc: RampError) -> ToolError:
    return ToolError(json.dumps(exc.to_dict(), default=str))


def build_server(settings: Settings | None = None, client: RampClient | None = None) -> _MCPServer:
    settings = settings or Settings.from_env()
    client = client or RampClient(settings)
    spec = load_spec()
    mcp = _MCPServer("ramp", instructions=INSTRUCTIONS)

    def check_allowed(method: str, path: str) -> None:
        m = method.upper()
        p = path.split("?", 1)[0].lower()
        if "/token" in p:
            raise ToolError("Token endpoints are managed by the server and can't be called directly.")
        if "/vault" in p and not settings.allow_vault:
            raise ToolError(
                "Card vault endpoints return sensitive card numbers and are disabled. "
                "Set RAMP_ALLOW_VAULT=true to enable them."
            )
        if m != "GET" and not settings.allow_writes:
            raise ToolError(
                f"{m} requests are disabled (read-only mode). Set RAMP_ALLOW_WRITES=true "
                "in the MCP server environment to allow creating, updating or deleting Ramp data."
            )

    # ------------------------------------------------------------ business
    @mcp.tool(structured_output=False)
    async def ramp_get_business(include_balance: bool = True) -> dict[str, Any]:
        """Get the Ramp business profile (name, entity info) and, optionally, its
        balance/credit limit information. Needs scope business:read."""
        try:
            out: dict[str, Any] = {"business": await client.request("GET", "/developer/v1/business")}
            if include_balance:
                out["balance"] = await client.request("GET", "/developer/v1/business/balance")
            return out
        except RampError as exc:
            raise _fail(exc) from exc

    # ---------------------------------------------------------- list tools
    def make_list_tool(name: str, path: str, label: str) -> None:
        filters = spec.query_param_names("GET", path)
        op = spec.find("GET", path)
        scopes = ", ".join(op.scopes) if op else ""

        async def list_tool(
            filters: dict[str, Any] | None = None,
            page_size: int = 25,
            max_pages: int = 1,
            next_url: str | None = None,
        ) -> dict[str, Any]:
            page_size = max(2, min(int(page_size), 100))
            max_pages = max(1, min(int(max_pages), 50))
            try:
                if next_url:
                    return await client.paginate(next_url, max_pages=max_pages)
                params = {**(filters or {}), "page_size": page_size}
                return await client.paginate(path, params=params, max_pages=max_pages)
            except RampError as exc:
                raise _fail(exc) from exc

        filter_text = ", ".join(filters) if filters else "none"
        doc = (
            f"List Ramp {label} (GET {path}).\n\n"
            f"Args:\n"
            f"  filters: optional query filters. Supported: {filter_text}.\n"
            f"  page_size: items per page (2-100, default 25).\n"
            f"  max_pages: follow pagination for up to this many pages (default 1, max 50).\n"
            f"  next_url: the `next` value from a previous call to continue paging (filters are then ignored).\n"
            f"Use ramp_describe_endpoint('GET', '{path}') for filter types and allowed values."
        )
        if scopes:
            doc += f"\nRequired scope: {scopes}."
        mcp.add_tool(list_tool, name=f"ramp_list_{name}", description=doc, structured_output=False)

    for name, (path, label) in LIST_RESOURCES.items():
        make_list_tool(name, path, label)

    # ------------------------------------------------------------ get tool
    @mcp.tool(structured_output=False)
    async def ramp_get(resource: GetResource, id: str, query: dict[str, Any] | None = None) -> Any:
        """Fetch a single Ramp object by its ID, e.g. resource="transaction",
        id="<uuid>". `query` passes optional query params (e.g. for transactions
        {"include_merchant_data": true}). Memo IDs are transaction IDs."""
        template = GET_RESOURCES.get(resource)
        if not template:
            raise ToolError(f"Unknown resource {resource!r}. Options: {', '.join(GET_RESOURCES)}")
        if not id or "/" in id or "?" in id:
            raise ToolError("id must be a plain Ramp object ID.")
        try:
            return await client.request("GET", template.format(id=id), params=query)
        except RampError as exc:
            raise _fail(exc) from exc

    # ----------------------------------------------------------- discovery
    @mcp.tool(structured_output=False)
    async def ramp_search_endpoints(query: str = "", method: str | None = None, limit: int = 25) -> dict[str, Any]:
        """Search the Ramp Developer API (all ~280 endpoints) by keyword, e.g.
        "approve bill", "card limits", "custom records", "webhook". Returns
        method, path, summary and required OAuth scopes. With an empty query,
        returns the list of API areas (tags) with endpoint counts."""
        if not query.strip():
            return {"areas": spec.tags(), "total_endpoints": len(spec.operations)}
        results = spec.search(query, method=method, limit=max(1, min(limit, 100)))
        return {"results": results, "count": len(results)}

    @mcp.tool(structured_output=False)
    async def ramp_describe_endpoint(method: str, path: str, schema_depth: int = 3) -> dict[str, Any]:
        """Show an endpoint's parameters, request body schema and response schema
        from Ramp's OpenAPI spec. `path` may be the template
        (/developer/v1/bills/{bill_id}) or a concrete path. Increase
        `schema_depth` (max 6) to expand nested objects further."""
        op = spec.find(method, path)
        if not op:
            raise ToolError(f"No endpoint {method.upper()} {path}. Use ramp_search_endpoints to find it.")
        return spec.describe(op, max_depth=max(1, min(schema_depth, 6)))

    @mcp.tool(structured_output=False)
    async def ramp_api_request(
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        max_pages: int = 1,
    ) -> Any:
        """Call any Ramp Developer API endpoint, e.g. method="GET",
        path="/developer/v1/limits" or path="/developer/v1/bills/<id>".
        Paths may omit the /developer/v1 prefix. For GET list endpoints,
        `max_pages` > 1 follows pagination and merges results.
        Look up the exact path and body with ramp_search_endpoints and
        ramp_describe_endpoint first. Writes (POST/PUT/PATCH/DELETE) move real
        money or change real data in production and are only allowed when the
        server runs with RAMP_ALLOW_WRITES=true - confirm with the user first."""
        check_allowed(method, path)
        try:
            if method == "GET" and max_pages > 1:
                return await client.paginate(path, params=query, max_pages=min(max_pages, 50))
            return await client.request(method, path, params=query, json=body)
        except RampError as exc:
            raise _fail(exc) from exc

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Ramp Developer API MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = build_server()
    if args.transport == "stdio":
        server.run()
    elif _SDK_V2:
        server.run(transport=args.transport, host=args.host, port=args.port)
    else:
        server.settings.host = args.host  # type: ignore[attr-defined]
        server.settings.port = args.port  # type: ignore[attr-defined]
        server.run(transport=args.transport)


if __name__ == "__main__":
    main()
