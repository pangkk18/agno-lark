import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import respx

from agno_lark.data import (
    LarkDataClient,
    LarkDataTools,
    LarkLingoTools,
    LarkWikiTools,
    LingoListResult,
)


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        feishu_app_id="cli_test",
        feishu_app_secret=SimpleNamespace(get_secret_value=lambda: "secret"),
        lark_api_base_url="https://open.example.test",
        lark_api_timeout=5,
        lark_data_max_rows=2,
        lark_data_max_cells=4,
        lark_data_max_chars=12,
        lark_base_page_size=50,
    )


def auth_route() -> respx.Route:
    return respx.post(
        "https://open.example.test/open-apis/auth/v3/tenant_access_token/internal"
    ).respond(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})


@pytest.mark.asyncio
@respx.mock
async def test_wiki_resolve_list_and_docx_read_use_official_sdk_with_explicit_token() -> None:
    auth = auth_route()
    resolve = respx.get("https://open.example.test/open-apis/wiki/v2/spaces/get_node").respond(
        200,
        json={
            "code": 0,
            "data": {
                "node": {
                    "node_token": "wikcnNode",
                    "obj_token": "doccnDoc",
                    "obj_type": "docx",
                    "space_id": "7346089688715935772",
                }
            },
        },
    )
    nodes = respx.get(
        "https://open.example.test/open-apis/wiki/v2/spaces/7346089688715935772/nodes"
    ).respond(
        200,
        json={
            "code": 0,
            "data": {
                "items": [{"node_token": "n1"}, {"node_token": "n2"}, {"node_token": "n3"}],
                "page_token": "next-page",
                "has_more": True,
            },
        },
    )
    raw = respx.get(
        "https://open.example.test/open-apis/docx/v1/documents/doccnDoc/raw_content"
    ).respond(200, json={"code": 0, "data": {"content": "abcdefghijklmnop"}})
    data = LarkDataClient(settings())

    node = await data.resolve_wiki_node("https://tenant.feishu.cn/wiki/wikcnNode?from=copy")
    listed = await data.list_wiki_nodes("7346089688715935772", page_size=20)
    result_dict = await LarkWikiTools(data).read("wikcnNode")

    assert node["obj_type"] == "docx"
    assert listed.nodes == [{"node_token": "n1"}, {"node_token": "n2"}]
    assert (listed.page_token, listed.has_more, listed.truncated) == ("next-page", True, True)
    assert result_dict["content"] == "abcdefghijkl"
    assert result_dict["truncated"] is True
    assert auth.call_count == 1
    for route in (resolve, nodes, raw):
        assert route.called
        assert route.calls[0].request.headers["Authorization"] == "Bearer tenant-token"
    assert resolve.calls[0].request.url.params["token"] == "wikcnNode"
    assert nodes.calls[0].request.url.params["page_size"] == "2"


@pytest.mark.asyncio
@respx.mock
async def test_wiki_sheet_and_base_delegate_without_confusing_node_and_object_tokens() -> None:
    auth_route()
    resolve = respx.get("https://open.example.test/open-apis/wiki/v2/spaces/get_node")
    resolve.side_effect = [
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "shtObject", "obj_type": "sheet"}}},
        ),
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "appObject", "obj_type": "bitable"}}},
        ),
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "appObject", "obj_type": "bitable"}}},
        ),
    ]
    sheet = respx.get(
        "https://open.example.test/open-apis/sheets/v2/spreadsheets/shtObject/values/Sheet1!A1:A1"
    ).respond(200, json={"code": 0, "data": {"valueRange": {"values": [["ok"]]}}})
    records = respx.get(
        "https://open.example.test/open-apis/bitable/v1/apps/appObject/tables/tblabc/records"
    ).respond(200, json={"code": 0, "data": {"items": [{"record_id": "rec1"}]}})
    data = LarkDataClient(settings())

    sheet_result = await data.read_sheet_values(
        "https://tenant.feishu.cn/wiki/wikiSheet", "Sheet1!A1:A1"
    )
    base_result = await data.list_base_records(
        "https://tenant.feishu.cn/wiki/wikiBase", "tblabc"
    )

    assert sheet_result.spreadsheet_token == "shtObject"
    assert base_result.app_token == "appObject"
    assert sheet.called and records.called
    with pytest.raises(ValueError, match="does not contain a sheet"):
        await data.read_sheet_values("https://tenant.feishu.cn/wiki/wikiBase", "A1:A1")


@pytest.mark.asyncio
@respx.mock
async def test_lingo_list_get_search_preserve_pagination_and_request_shape() -> None:
    auth_route()
    repos = respx.get("https://open.example.test/open-apis/lingo/v1/repos").respond(
        200, json={"code": 0, "data": {"items": [{"id": 7346089688715935772, "name": "Terms"}]}}
    )
    classifications = respx.get(
        "https://open.example.test/open-apis/lingo/v1/classifications"
    ).respond(
        200,
        json={
            "code": 0,
            "data": {
                "items": [{"id": "secondary", "name": "Product", "father_id": "primary"}],
                "page_token": "class-next",
                "has_more": True,
            },
        },
    )
    entities = respx.get("https://open.example.test/open-apis/lingo/v1/entities").respond(
        200,
        json={
            "code": 0,
            "data": {"entities": [{"id": "e1"}], "page_token": "entity-next", "has_more": True},
        },
    )
    entity = respx.get("https://open.example.test/open-apis/lingo/v1/entities/e1").respond(
        200,
        json={"code": 0, "data": {"entity": {"id": "e1", "main_keys": [{"key": "Agno"}]}}},
    )
    search = respx.post("https://open.example.test/open-apis/lingo/v1/entities/search").respond(
        200,
        json={
            "code": 0,
            "data": {"entities": [{"id": "e1"}], "page_token": "search-next", "has_more": True},
        },
    )
    data = LarkDataClient(settings())

    assert (await data.list_lingo_repos()).items[0]["name"] == "Terms"
    cls = await data.list_lingo_classifications("7346089688715935772", page_size=100)
    listed = await data.list_lingo_entities("7346089688715935772", page_token="page1")
    assert (await data.get_lingo_entity("e1"))["id"] == "e1"
    found = await data.search_lingo_entities(
        "Agno",
        repo_id="7346089688715935772",
        classification_ids=["secondary"],
        page_token="page2",
    )

    assert (cls.page_token, cls.has_more) == ("class-next", True)
    assert (listed.page_token, listed.has_more) == ("entity-next", True)
    assert (found.page_token, found.has_more) == ("search-next", True)
    assert classifications.calls[0].request.url.params["repo_id"] == "7346089688715935772"
    assert classifications.calls[0].request.url.params["page_size"] == "2"
    assert entities.calls[0].request.url.params["page_token"] == "page1"
    assert search.calls[0].request.url.params["repo_id"] == "7346089688715935772"
    assert search.calls[0].request.url.params["page_token"] == "page2"
    assert json.loads(search.calls[0].request.content) == {
        "query": "Agno",
        "classification_filter": {"include": ["secondary"]},
    }
    for route in (repos, classifications, entities, entity, search):
        assert route.calls[0].request.headers["Authorization"] == "Bearer tenant-token"


@pytest.mark.asyncio
@respx.mock
async def test_lingo_category_verifies_repo_and_resolves_primary_to_secondary() -> None:
    auth_route()
    respx.get("https://open.example.test/open-apis/lingo/v1/repos").respond(
        200, json={"code": 0, "data": {"items": [{"id": 7346089688715935772, "name": "Terms"}]}}
    )
    respx.get("https://open.example.test/open-apis/lingo/v1/classifications").respond(
        200,
        json={
            "code": 0,
            "data": {
                "items": [
                    {"id": "primary", "father_id": "0"},
                    {"id": "secondary-a", "father_id": "primary"},
                    {"id": "secondary-b", "father_id": "primary"},
                ]
            },
        },
    )
    search = respx.post("https://open.example.test/open-apis/lingo/v1/entities/search").respond(
        200, json={"code": 0, "data": {"entities": [{"id": "e1"}]}}
    )
    data = LarkDataClient(settings(), max_rows=10)
    result = await data.read_lingo_category(
        "https://tenant.feishu.cn/lingo/category?unit_id=7346089688715935772&primary_class=primary",
        keyword="Agno",
    )
    assert result.items == [{"id": "e1"}]
    assert json.loads(search.calls[0].request.content)["classification_filter"]["include"] == [
        "secondary-a",
        "secondary-b",
    ]
    category_only = await data.read_lingo_category(
        "https://tenant.feishu.cn/lingo/category?unit_id=7346089688715935772&primary_class=primary"
    )
    assert category_only.items == [{"id": "e1"}]
    assert json.loads(search.calls[1].request.content) == {
        "classification_filter": {"include": ["secondary-a", "secondary-b"]}
    }


@pytest.mark.asyncio
@respx.mock
async def test_lingo_category_never_falls_back_to_global_search_when_resolution_fails() -> None:
    auth_route()
    respx.get("https://open.example.test/open-apis/lingo/v1/repos").respond(
        200, json={"code": 0, "data": {"items": [{"id": 1, "name": "Other"}]}}
    )
    search = respx.post("https://open.example.test/open-apis/lingo/v1/entities/search").respond(
        200, json={"code": 0, "data": {"entities": []}}
    )
    data = LarkDataClient(settings())
    with pytest.raises(ValueError, match="accessible Lingo repository"):
        await data.read_lingo_category(
            "https://tenant.feishu.cn/lingo/category?unit_id=7346089688715935772&primary_class=primary",
            keyword="Agno",
        )
    assert not search.called


def test_wiki_lingo_validation_and_toolkit_registration() -> None:
    data = LarkDataClient(settings())
    with pytest.raises(ValueError, match="Lingo category URL"):
        data._parse_lingo_category("https://evil.example/anything?unit_id=1&primary_class=2")
    for toolkit in (LarkWikiTools(data), LarkLingoTools(data), LarkDataTools(data)):
        names = set(toolkit.async_functions)
        assert names
    assert {"resolve_wiki_node", "list_wiki_nodes", "read_wiki"} <= set(
        LarkWikiTools(data).async_functions
    )
    assert {
        "list_lingo_repos",
        "list_lingo_classifications",
        "list_lingo_entities",
        "get_lingo_entity",
        "search_lingo_entities",
        "read_lingo_category",
    } <= set(LarkLingoTools(data).async_functions)
    assert "read_wiki" in LarkDataTools(data).async_functions
    assert "search_lingo_entities" in LarkDataTools(data).async_functions


@pytest.mark.asyncio
@respx.mock
async def test_wiki_requires_type_specific_selector_and_rejects_unsupported_type() -> None:
    auth_route()
    resolve = respx.get("https://open.example.test/open-apis/wiki/v2/spaces/get_node")
    resolve.side_effect = [
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "shtObject", "obj_type": "sheet"}}},
        ),
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "appObject", "obj_type": "bitable"}}},
        ),
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "docObject", "obj_type": "file"}}},
        ),
    ]
    data = LarkDataClient(settings())
    with pytest.raises(ValueError, match="cell_range is required"):
        await data.read_wiki("wikiSheet")
    with pytest.raises(ValueError, match="table_id is required"):
        await data.read_wiki("wikiBase")
    with pytest.raises(ValueError, match="unsupported Wiki object type: file"):
        await data.read_wiki("wikiFile")


def test_opaque_page_tokens_are_bounded_but_not_identifier_validated() -> None:
    assert LarkDataClient._page_token("opaque+/=cursor") == "opaque+/=cursor"
    assert LarkDataClient(settings())._page_size(999, api_max=50) == 2
    with pytest.raises(ValueError, match="page token"):
        LarkDataClient._page_token("bad\nvalue")


@pytest.mark.asyncio
@respx.mock
async def test_read_wiki_sheet_and_base_branches_return_bounded_payloads() -> None:
    auth_route()
    resolve = respx.get("https://open.example.test/open-apis/wiki/v2/spaces/get_node")
    resolve.side_effect = [
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "shtObject", "obj_type": "sheet"}}},
        ),
        respx.MockResponse(
            200,
            json={"code": 0, "data": {"node": {"obj_token": "appObject", "obj_type": "bitable"}}},
        ),
    ]
    respx.get(
        "https://open.example.test/open-apis/sheets/v2/spreadsheets/shtObject/values/Sheet1!A1:A1"
    ).respond(200, json={"code": 0, "data": {"valueRange": {"values": [["ok"]]}}})
    respx.get(
        "https://open.example.test/open-apis/bitable/v1/apps/appObject/tables/tblabc/records"
    ).respond(
        200,
        json={
            "code": 0,
            "data": {"items": [{"record_id": "rec1"}], "page_token": "next", "has_more": True},
        },
    )
    data = LarkDataClient(settings())
    sheet = await data.read_wiki("wikiSheet", cell_range="Sheet1!A1:A1")
    base = await data.read_wiki("wikiBase", table_id="tblabc")
    assert sheet.values == [["ok"]]
    assert base.records == [{"record_id": "rec1"}]
    assert (base.page_token, base.has_more) == ("next", True)


@pytest.mark.asyncio
async def test_fake_sdk_api_error_exposes_only_safe_code() -> None:
    auth = AsyncMock(
        return_value={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}
    )
    repos = SimpleNamespace(
        alist=AsyncMock(return_value=SimpleNamespace(code=190001, msg="private response"))
    )
    client = SimpleNamespace(
        auth=SimpleNamespace(
            v3=SimpleNamespace(tenant_access_token=SimpleNamespace(ainternal=auth))
        ),
        bitable=SimpleNamespace(v1=SimpleNamespace()),
        lingo=SimpleNamespace(v1=SimpleNamespace(repo=repos)),
    )
    data = LarkDataClient(settings(), client=client)
    with pytest.raises(RuntimeError, match="code=190001") as error:
        await data.list_lingo_repos()
    assert "private response" not in str(error.value)
    assert "tenant-token" not in str(error.value)

    repos.alist.return_value = SimpleNamespace(
        code=None, msg="private malformed response", success=lambda: False
    )
    with pytest.raises(RuntimeError, match="code=unknown") as malformed:
        await data.list_lingo_repos()
    assert "private malformed response" not in str(malformed.value)


@pytest.mark.asyncio
@respx.mock
async def test_base_null_fields_survive_normalization() -> None:
    auth_route()
    respx.get(
        "https://open.example.test/open-apis/bitable/v1/apps/appObject/tables/tblabc/records"
    ).respond(
        200,
        json={
            "code": 0,
            "data": {"items": [{"record_id": "rec1", "fields": {"optional": None}}]},
        },
    )
    result = await LarkDataClient(settings()).list_base_records("appObject", "tblabc")
    assert result.records[0]["fields"] == {"optional": None}


@pytest.mark.asyncio
@respx.mock
async def test_lingo_missing_entity_is_an_error_and_truncation_is_visible() -> None:
    auth_route()
    entity = respx.get("https://open.example.test/open-apis/lingo/v1/entities/e1")
    entity.respond(200, json={"code": 0, "data": {"entity": None}})
    data = LarkDataClient(settings())
    with pytest.raises(RuntimeError, match="incomplete"):
        await data.get_lingo_entity("e1")
    entity.respond(
        200, json={"code": 0, "data": {"entity": {"id": "e1", "description": "x" * 40}}}
    )
    result = await data.get_lingo_entity("e1")
    assert result["id"] == "e1"
    assert len(result["description"]) == 12
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_registered_combined_tool_forwards_scoped_pagination() -> None:
    search = AsyncMock(
        return_value=LingoListResult(
            items=[{"id": "e1", "description": "Definition"}],
            page_token="next+/=", has_more=True,
        )
    )
    toolkit = LarkDataTools(SimpleNamespace(search_lingo_entities=search))
    function = toolkit.get_async_functions()["search_lingo_entities"]
    result = await function.entrypoint(
        keyword="Agno", repo_id="123", classification_ids=["456"],
        page_token="first+/=", page_size=5,
    )
    search.assert_awaited_once_with(
        "Agno", repo_id="123", classification_ids=["456"],
        page_token="first+/=", page_size=5,
    )
    assert result["items"][0]["id"] == "e1"
    assert result["page_token"] == "next+/="
    assert result["has_more"] is True
