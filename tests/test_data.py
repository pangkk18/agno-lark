import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx

from agno_lark.data import (
    LarkDataClient,
    build_lark_data_client,
    parse_base_token,
    parse_sheet_token,
    validate_cell_range,
)
from agno_lark.interface import LarkInterface


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        feishu_app_id="cli_test",
        feishu_app_secret=SimpleNamespace(get_secret_value=lambda: "secret"),
        lark_api_base_url="https://open.example.test",
        lark_api_timeout=5,
        lark_data_max_rows=2,
        lark_data_max_cells=4,
        lark_base_page_size=50,
    )


def test_resource_parsing_and_range_validation() -> None:
    assert parse_sheet_token("https://open.feishu.cn/sheets/shtabc") == "shtabc"
    assert parse_base_token("https://open.feishu.cn/base/appabc") == "appabc"
    assert validate_cell_range("Sheet1!A1:D2") == "Sheet1!A1:D2"
    with pytest.raises(ValueError):
        validate_cell_range("Sheet1")


def test_factory_does_not_reuse_message_channel_client() -> None:
    channel = SimpleNamespace(client=object())
    data = build_lark_data_client(settings(), channel=channel)
    assert data.client is not channel.client
    assert callable(data.client.auth.v3.tenant_access_token.ainternal)
    assert callable(data.client.bitable.v1.app_table_record.alist)
    assert data.client._config.timeout == 5
    assert data.client._config.enable_set_token is True


def test_interface_builds_independent_data_client_and_preserves_explicit_injection() -> None:
    channel = SimpleNamespace(client=object())
    config = settings()
    config.target_type = "agent"
    config.target_id = "assistant"
    interface = LarkInterface(target=object(), settings=config, channel=channel)
    assert interface.data_client.client is not channel.client
    assert interface.bridge.data_client is interface.data_client

    custom = object()
    interface = LarkInterface(target=object(), settings=config, channel=channel, data_client=custom)
    assert interface.data_client is custom
    assert interface.bridge.data_client is custom


def test_incompatible_injected_client_fails_at_construction() -> None:
    with pytest.raises(TypeError, match="lark_oapi"):
        LarkDataClient(settings(), client=SimpleNamespace())

    from lark_oapi import Client

    sdk = Client.builder().app_id("cli_test").app_secret("secret").build()
    with pytest.raises(TypeError, match="enable_set_token"):
        LarkDataClient(settings(), client=sdk)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("nested", [False, True])
async def test_default_interface_reads_sheets_and_base_without_sync_auth(
    monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    """Exercise real SDK serialization and authentication, with HTTP intercepted."""
    sync_request = Mock(side_effect=AssertionError("blocking HTTP request"))
    monkeypatch.setattr("requests.request", sync_request)
    config = settings()
    config.target_type = "agent"
    config.target_id = "assistant"
    interface = LarkInterface(
        target=object(), settings=config, channel=SimpleNamespace(client=object())
    )
    data = interface.data_client
    token_body = {"tenant_access_token": "tenant-token", "expire": 7200}
    auth = respx.post(
        "https://open.example.test/open-apis/auth/v3/tenant_access_token/internal"
    ).respond(200, json={"code": 0, **({"data": token_body} if nested else token_body)})
    sheet = respx.get(
        "https://open.example.test/open-apis/sheets/v2/spreadsheets/shtabc/values/Sheet1!A1:B2"
    ).respond(200, json={"code": 0, "data": {"valueRange": {"values": [["name"], ["Ada"]]}}})
    tables = respx.get("https://open.example.test/open-apis/bitable/v1/apps/appabc/tables").respond(
        200, json={"code": 0, "data": {"items": [{"table_id": "tblabc"}]}}
    )
    records = respx.get(
        "https://open.example.test/open-apis/bitable/v1/apps/appabc/tables/tblabc/records"
    ).respond(
        200, json={"code": 0, "data": {"items": [{"record_id": "rec1", "fields": {"name": "Ada"}}]}}
    )
    search = respx.post(
        "https://open.example.test/open-apis/bitable/v1/apps/appabc/tables/tblabc/records/search"
    ).respond(200, json={"code": 0, "data": {"items": [{"record_id": "rec1"}]}})

    assert (await data.read_sheet_values("shtabc", "Sheet1!A1:B2")).values == [["name"], ["Ada"]]
    assert (await data.list_base_tables("appabc")).tables[0]["table_id"] == "tblabc"
    assert (await data.list_base_records("appabc", "tblabc")).records[0]["record_id"] == "rec1"
    assert (await data.search_base_records("appabc", "tblabc", "Ada")).records[0][
        "record_id"
    ] == "rec1"
    assert auth.call_count == 1
    for route in (sheet, tables, records, search):
        assert route.called
        assert route.calls[0].request.headers["Authorization"] == "Bearer tenant-token"
    sync_request.assert_not_called()


def token_client(auth: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        auth=SimpleNamespace(
            v3=SimpleNamespace(tenant_access_token=SimpleNamespace(ainternal=auth))
        ),
        bitable=SimpleNamespace(v1=SimpleNamespace()),
    )


@pytest.mark.asyncio
async def test_tenant_token_refreshes_before_expiry_and_shares_concurrent_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Mock(return_value=100.0)
    monkeypatch.setattr("agno_lark.data.time.monotonic", clock)
    tokens = iter(["token-1", "token-2"])

    async def fetch(_request: object) -> SimpleNamespace:
        await asyncio.sleep(0)
        return SimpleNamespace(
            code=0, data=SimpleNamespace(tenant_access_token=next(tokens), expire=120)
        )

    auth = AsyncMock(side_effect=fetch)
    data = LarkDataClient(settings(), client=token_client(auth))
    assert await data._access_token() == "token-1"
    clock.return_value = 150.0
    assert await data._access_token() == "token-1"
    assert auth.await_count == 1
    clock.return_value = 161.0
    assert await asyncio.gather(*(data._access_token() for _ in range(5))) == ["token-2"] * 5
    assert auth.await_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_real_sdk_auth_failure_prevents_resource_request() -> None:
    auth = respx.post(
        "https://open.example.test/open-apis/auth/v3/tenant_access_token/internal"
    ).respond(200, json={"code": 10003, "msg": "private-secret"})
    data = build_lark_data_client(settings())
    with pytest.raises(RuntimeError, match="authentication failed.*10003") as error:
        await data.read_sheet_values("shtabc", "Sheet1!A1:A2")
    assert "private-secret" not in str(error.value)
    assert auth.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_base_search_fallback_uses_explicit_token() -> None:
    respx.post("https://open.example.test/open-apis/auth/v3/tenant_access_token/internal").respond(
        200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}
    )
    respx.post(
        "https://open.example.test/open-apis/bitable/v1/apps/appabc/tables/tblabc/records/search"
    ).respond(200, json={"code": 1254045, "msg": "field not found"})
    records = respx.get(
        "https://open.example.test/open-apis/bitable/v1/apps/appabc/tables/tblabc/records"
    ).respond(
        200, json={"code": 0, "data": {"items": [{"record_id": "rec1", "fields": {"name": "Ada"}}]}}
    )
    data = build_lark_data_client(settings())
    result = await data.search_base_records("appabc", "tblabc", "Ada")
    assert result.records[0]["record_id"] == "rec1"
    assert records.calls[0].request.headers["Authorization"] == "Bearer tenant-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,match",
    [
        ({"code": 10003, "msg": "private-secret"}, "authentication failed.*10003"),
        ({"code": 0, "data": {}}, "token response was empty"),
        ({"code": 0, "data": {"tenant_access_token": "tenant-token", "expire": 0}}, "expiry"),
    ],
)
async def test_authentication_failure_is_not_cached_or_exposed(body: dict, match: str) -> None:
    auth = AsyncMock(return_value=body)
    data = LarkDataClient(settings(), client=token_client(auth))
    for _ in range(2):
        with pytest.raises(RuntimeError, match=match) as error:
            await data._access_token()
        assert "private-secret" not in str(error.value)
        assert "tenant-token" not in str(error.value)
    assert auth.await_count == 2


@pytest.mark.asyncio
async def test_sheet_values_are_bounded_and_use_bearer() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": {"valueRange": {"values": [[1, 2, 3], [4, 5], [6]]}}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        auth = AsyncMock(
            return_value={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}
        )
        client = LarkDataClient(settings(), client=token_client(auth), http_client=http)
        result = await client.read_sheet_values("shtabc", "Sheet1!A1:C3")
    assert result.values == [[1, 2, 3], [4]]
    assert result.truncated is True
    assert requests[0].headers["Authorization"] == "Bearer tenant-token"


@pytest.mark.asyncio
async def test_base_records_normalize_sdk_response() -> None:
    class Records:
        async def alist(self, request: object, option: object) -> object:
            return SimpleNamespace(data=SimpleNamespace(items=[SimpleNamespace(record_id="rec1")]))

    auth = AsyncMock(
        return_value={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}
    )
    client = token_client(auth)
    client.bitable.v1.app_table_record = Records()
    data = LarkDataClient(settings(), client=client)
    result = await data.list_base_records("appabc", "tblabc")
    assert result.records == [{"record_id": "rec1"}]
