import json

import httpx
import pytest
import respx

from ramp_mcp.client import RampClient
from ramp_mcp.config import Settings
from ramp_mcp.server import GET_RESOURCES, LIST_RESOURCES, ToolError, build_server
from ramp_mcp.spec import load_spec

BASE = "https://demo-api.ramp.com"


def make(allow_writes=False, allow_vault=False):
    settings = Settings(
        environment="sandbox",
        base_url=BASE,
        client_id="cid",
        client_secret="secret",
        scopes="transactions:read",
        allow_writes=allow_writes,
        allow_vault=allow_vault,
        max_retries=2,
    )
    client = RampClient(settings)
    return build_server(settings, client), client


def text_of(result):
    content = result.content if hasattr(result, "content") else result
    if isinstance(content, tuple):
        content = content[0]
    return json.loads(content[0].text)


def mock_token(router):
    return router.post(f"{BASE}/developer/v1/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 864000})
    )


def test_curated_paths_exist_in_spec():
    spec = load_spec()
    for path, _ in LIST_RESOURCES.values():
        assert spec.find("GET", path), path
    for template in GET_RESOURCES.values():
        assert spec.find("GET", template.format(id="abc")), template


async def test_list_tools_registered():
    server, _ = make()
    names = {t.name for t in await server.list_tools()}
    assert {"ramp_list_transactions", "ramp_get", "ramp_api_request", "ramp_search_endpoints"} <= names


@respx.mock
async def test_pagination_and_token_caching():
    token = mock_token(respx)
    page2 = f"{BASE}/developer/v1/transactions?page_size=2&start=abc"
    respx.get(f"{BASE}/developer/v1/transactions", params={"start": "abc"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "3"}], "page": {"next": None}})
    )
    first = respx.get(f"{BASE}/developer/v1/transactions", params={"page_size": "2"}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "1"}, {"id": "2"}], "page": {"next": page2}})
    )
    server, _ = make()
    out = text_of(
        await server.call_tool(
            "ramp_list_transactions", {"filters": {"state": "CLEARED"}, "page_size": 2, "max_pages": 5}
        )
    )
    assert [d["id"] for d in out["data"]] == ["1", "2", "3"]
    assert out["next"] is None and out["pages_fetched"] == 2
    assert token.call_count == 1
    req = first.calls[0].request
    assert req.headers["authorization"] == "Bearer tok"
    assert req.url.params["state"] == "CLEARED"
    # token request uses client credentials with Basic auth
    assert token.calls[0].request.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in token.calls[0].request.content


@respx.mock
async def test_writes_blocked_by_default():
    server, _ = make()
    with pytest.raises(ToolError, match="read-only"):
        await server.call_tool("ramp_api_request", {"method": "POST", "path": "/developer/v1/bills", "body": {}})


@respx.mock
async def test_writes_allowed_when_enabled():
    mock_token(respx)
    route = respx.patch(f"{BASE}/developer/v1/bills/b1").mock(return_value=httpx.Response(200, json={"id": "b1"}))
    server, _ = make(allow_writes=True)
    out = text_of(
        await server.call_tool(
            "ramp_api_request", {"method": "PATCH", "path": "bills/b1", "body": {"memo": "x"}}
        )
    )
    assert out == {"id": "b1"}
    assert json.loads(route.calls[0].request.content) == {"memo": "x"}


async def test_vault_blocked():
    server, _ = make(allow_writes=True)
    with pytest.raises(ToolError, match="vault"):
        await server.call_tool("ramp_api_request", {"method": "GET", "path": "/developer/v1/cards/vault/c1"})


async def test_foreign_host_rejected():
    server, _ = make()
    with pytest.raises(ToolError, match="Refusing"):
        await server.call_tool(
            "ramp_list_transactions", {"next_url": "https://evil.example.com/developer/v1/transactions"}
        )


@respx.mock
async def test_rate_limit_retry(monkeypatch):
    import ramp_mcp.client as c

    async def no_sleep(_):
        return None

    monkeypatch.setattr(c.asyncio, "sleep", no_sleep)
    mock_token(respx)
    respx.get(f"{BASE}/developer/v1/business").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"name": "Acme"})]
    )
    server, _ = make()
    out = text_of(await server.call_tool("ramp_get_business", {"include_balance": False}))
    assert out["business"]["name"] == "Acme"


@respx.mock
async def test_error_surfaces_ramp_message():
    mock_token(respx)
    respx.get(f"{BASE}/developer/v1/transactions/t1").mock(
        return_value=httpx.Response(
            404,
            headers={"x-trace-id": "trace123"},
            json={"error_v2": {"error_code": "DEVELOPER_7003", "message": "Not found"}},
        )
    )
    server, _ = make()
    with pytest.raises(ToolError) as exc:
        await server.call_tool("ramp_get", {"resource": "transaction", "id": "t1"})
    msg = str(exc.value)
    data = json.loads(msg[msg.index("{"):])
    assert data["status"] == 404 and "DEVELOPER_7003" in data["error"] and data["x_trace_id"] == "trace123"


async def test_search_and_describe():
    server, _ = make()
    out = text_of(await server.call_tool("ramp_search_endpoints", {"query": "list bills"}))
    assert any(r["path"] == "/developer/v1/bills" and r["method"] == "GET" for r in out["results"])
    desc = text_of(
        await server.call_tool("ramp_describe_endpoint", {"method": "GET", "path": "/developer/v1/bills/123"})
    )
    assert desc["path"] == "/developer/v1/bills/{bill_id}"
    assert any(p["name"] == "bill_id" for p in desc["parameters"])
