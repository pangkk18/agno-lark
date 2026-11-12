from types import SimpleNamespace

import httpx
import pytest

from agno_lark.data import (
    LarkDataClient,
    parse_base_token,
    parse_sheet_token,
    validate_cell_range,
)


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
        client = LarkDataClient(settings(), client=SimpleNamespace(), http_client=http)
        client._token = "tenant-token"
        result = await client.read_sheet_values("shtabc", "Sheet1!A1:C3")
    assert result.values == [[1, 2, 3], [4]]
    assert result.truncated is True
    assert requests[0].headers["Authorization"] == "Bearer tenant-token"


@pytest.mark.asyncio
async def test_base_records_normalize_sdk_response() -> None:
    class Records:
        async def alist(self, request: object) -> object:
            return SimpleNamespace(data=SimpleNamespace(items=[SimpleNamespace(record_id="rec1")]))

    client = SimpleNamespace(
        bitable=SimpleNamespace(v1=SimpleNamespace(app_table_record=Records()))
    )
    data = LarkDataClient(settings(), client=client)
    result = await data.list_base_records("appabc", "tblabc")
    assert result.records == [{"record_id": "rec1"}]
