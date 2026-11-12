from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agno_lark.bridge import LarkBridge
from agno_lark.cards import data_card, help_card

CATEGORY = "https://tenant.feishu.cn/lingo/category?unit_id=123&primary_class=456&theme=light"


def fixture_bridge(**methods):
    channel = SimpleNamespace(send=AsyncMock())
    data = SimpleNamespace(**methods)
    bridge = LarkBridge(
        target=SimpleNamespace(arun=AsyncMock()),
        settings=SimpleNamespace(
            target_type="agent",
            target_id="assistant",
            allowed_user_ids=set(),
            allowed_chat_ids=set(),
            lark_data_max_chars=500,
        ),
        store=SimpleNamespace(),
        channel=channel,
        data_client=data,
    )
    msg = SimpleNamespace(
        id="m-1",
        body_text="",
        sender=SimpleNamespace(open_id="ou_1", is_bot=False),
        conversation=SimpleNamespace(chat_id="oc_1", chat_type="p2p", thread_id=None),
    )
    return bridge, channel, data, msg


def page(**overrides):
    values = dict(items=[], nodes=[], page_token=None, has_more=False, truncated=False)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_wiki_document_command_returns_bounded_markdown_card():
    read = AsyncMock(
        return_value=SimpleNamespace(
            node={"title": "Welcome", "obj_token": "private-token"},
            content="Hello **Wiki**",
            values=None,
            records=None,
            truncated=True,
            has_more=False,
            page_token=None,
        )
    )
    bridge, channel, _, msg = fixture_bridge(read_wiki=read)
    msg.body_text = "/wiki https://tenant.feishu.cn/wiki/wikxxx"
    await bridge.handle_message(msg)
    read.assert_awaited_once_with("https://tenant.feishu.cn/wiki/wikxxx")
    assert channel.send.await_count == 1
    card = channel.send.call_args.args[1]["card"]
    assert card["schema"] == "2.0"
    assert "Hello **Wiki**" in str(card)
    assert "已截断" in str(card)
    assert "private-token" not in str(card)
    bridge.target.arun.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argument,expected",
    [
        ("sheet123!A1:D5", {"cell_range": "sheet123!A1:D5"}),
        ("tblxxx", {"table_id": "tblxxx"}),
    ],
)
async def test_wiki_sheet_or_base_argument(argument, expected):
    read = AsyncMock(
        return_value=SimpleNamespace(
            node={"title": "data"},
            content=None,
            values=[["name"], ["Ada"]],
            records=None,
            truncated=False,
            has_more=False,
            page_token=None,
        )
    )
    bridge, channel, _, msg = fixture_bridge(read_wiki=read)
    await bridge.handle_command(f"/wiki wikxxx {argument}", msg, "scope", "session")
    read.assert_awaited_once_with("wikxxx", **expected)
    assert "Ada" in str(channel.send.call_args)


@pytest.mark.asyncio
async def test_wiki_nodes_show_next_page_token():
    listing = AsyncMock(
        return_value=SimpleNamespace(
            nodes=[{"node_token": "node1", "title": "Intro", "obj_type": "docx"}],
            has_more=True,
            page_token="next",
            truncated=False,
        )
    )
    bridge, channel, _, msg = fixture_bridge(list_wiki_nodes=listing)
    await bridge.handle_command("/wiki ls 123 parent first", msg, "scope", "session")
    listing.assert_awaited_once_with("123", parent_node_token="parent", page_token="first")
    assert "Intro" in str(channel.send.call_args)
    assert "next" in str(channel.send.call_args)
    assert "下一页" in str(channel.send.call_args)


@pytest.mark.asyncio
async def test_lingo_plain_keyword_and_category_url_dispatch():
    result = SimpleNamespace(
        items=[
            {
                "id": "term1",
                "main_keys": [{"key": "API"}],
                "description": "Application programming interface",
            }
        ],
        has_more=False,
        page_token=None,
        truncated=False,
    )
    search = AsyncMock(return_value=result)
    category = AsyncMock(return_value=result)
    bridge, channel, _, msg = fixture_bridge(
        search_lingo_entities=search,
        read_lingo_category=category,
    )
    await bridge.handle_command("/lingo application programming", msg, "scope", "session")
    search.assert_awaited_once_with("application programming")
    await bridge.handle_command(f"/lingo {CATEGORY}", msg, "scope", "session")
    category.assert_awaited_once_with(CATEGORY)
    assert "Application programming interface" in str(channel.send.call_args)
    assert "unit_id" not in str(channel.send.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["repo123", CATEGORY])
async def test_lingo_scoped_search(resource):
    search, category = AsyncMock(return_value=page()), AsyncMock(return_value=page())
    bridge, _, _, msg = fixture_bridge(
        search_lingo_entities=search,
        read_lingo_category=category,
    )
    await bridge.handle_command(f"/lingo search {resource} API name", msg, "scope", "session")
    if resource == CATEGORY:
        category.assert_awaited_once_with(CATEGORY, keyword="API name")
        search.assert_not_awaited()
    else:
        search.assert_awaited_once_with("API name", repo_id="repo123")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,method,args,kwargs",
    [
        ("/lingo repos", "list_lingo_repos", (), {}),
        (
            "/lingo categories repo123 cursor",
            "list_lingo_classifications",
            ("repo123",),
            {"page_token": "cursor"},
        ),
        (
            "/lingo list repo123 cursor",
            "list_lingo_entities",
            ("repo123",),
            {"page_token": "cursor"},
        ),
        ("/lingo get term123", "get_lingo_entity", ("term123",), {}),
    ],
)
async def test_lingo_subcommands(command, method, args, kwargs):
    result = (
        {"id": "term123", "description": "Definition"} if method == "get_lingo_entity" else page()
    )
    call = AsyncMock(return_value=result)
    bridge, channel, _, msg = fixture_bridge(**{method: call})
    await bridge.handle_command(command, msg, "scope", "session")
    call.assert_awaited_once_with(*args, **kwargs)
    assert channel.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "/wiki",
        "/wiki ls",
        "/wiki token a b",
        "/lingo",
        "/lingo get",
        "/lingo search repo",
        "/lingo categories",
        "/lingo repos cursor",
        "/lingo API --page-token",
    ],
)
async def test_invalid_command_is_actionable(command):
    bridge, channel, _, msg = fixture_bridge()
    await bridge.handle_command(command, msg, "scope", "session")
    assert "用法" in str(channel.send.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,method,args,kwargs",
    [
        (
            "/wiki wikxxx tblabc --page-token next+/=",
            "read_wiki",
            ("wikxxx",),
            {"table_id": "tblabc", "page_token": "next+/="},
        ),
        (
            "/lingo API --page-token next+/=",
            "search_lingo_entities",
            ("API",),
            {"page_token": "next+/="},
        ),
        (
            f"/lingo {CATEGORY} --page-token next+/=",
            "read_lingo_category",
            (CATEGORY,),
            {"page_token": "next+/="},
        ),
        (
            "/lingo search 123 API --page-token next+/=",
            "search_lingo_entities",
            ("API",),
            {"repo_id": "123", "page_token": "next+/="},
        ),
        (
            "/wiki ls 123 --page-token next+/=",
            "list_wiki_nodes",
            ("123",),
            {"page_token": "next+/="},
        ),
    ],
)
async def test_next_page_flags_preserve_original_query(command, method, args, kwargs):
    result = SimpleNamespace(
        node={},
        content=None,
        values=None,
        records=None,
        items=[],
        nodes=[],
        has_more=False,
        page_token=None,
        truncated=False,
    )
    call = AsyncMock(return_value=result)
    bridge, _, _, msg = fixture_bridge(**{method: call})
    await bridge.handle_command(command, msg, "scope", "session")
    call.assert_awaited_once_with(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,method,args,result",
    [
        (
            "/sheet https://tenant.feishu.cn/wiki/node1 Sheet1!A1:B2",
            "read_sheet_values",
            ("https://tenant.feishu.cn/wiki/node1", "Sheet1!A1:B2"),
            SimpleNamespace(values=[["name"], ["Ada"]], truncated=False),
        ),
        (
            "/base https://tenant.feishu.cn/wiki/node2 tblabc",
            "list_base_records",
            ("https://tenant.feishu.cn/wiki/node2", "tblabc"),
            SimpleNamespace(records=[{"fields": {"name": "Ada"}}], truncated=False),
        ),
    ],
)
async def test_existing_data_commands_pass_wiki_urls_to_data_client(command, method, args, result):
    call = AsyncMock(return_value=result)
    bridge, channel, _, msg = fixture_bridge(**{method: call})
    await bridge.handle_command(command, msg, "scope", "session")
    call.assert_awaited_once_with(*args)
    assert "Ada" in str(channel.send.call_args)


@pytest.mark.asyncio
async def test_permissions_and_dedup_are_applied_before_data_access():
    search = AsyncMock(return_value=page())
    bridge, channel, _, msg = fixture_bridge(search_lingo_entities=search)
    msg.body_text = "/lingo API"
    bridge.settings.allowed_user_ids = {"someone_else"}
    await bridge.handle_message(msg)
    search.assert_not_awaited()
    bridge.settings.allowed_user_ids = set()
    bridge.store.seen_message = AsyncMock(return_value=True)
    await bridge.handle_message(msg)
    search.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_lingo_api_error_does_not_expose_response_or_secrets():
    search = AsyncMock(side_effect=RuntimeError("private-token; app-secret"))
    bridge, channel, _, msg = fixture_bridge(search_lingo_entities=search)
    await bridge.handle_command("/lingo API", msg, "scope", "session")
    assert "请检查资源权限和配置" in str(channel.send.call_args)
    assert "private-token" not in str(channel.send.call_args)
    assert bridge.last_error == "data read failed: RuntimeError"


def test_cards_include_commands_and_support_markdown_content_and_pagination():
    assert "/wiki" in str(help_card())
    assert "/lingo" in str(help_card())
    card = data_card(
        title="Wiki", headers=[], rows=[], content="**body**", has_more=True, page_token="next"
    )
    assert "**body**" in str(card)
    assert "下一页" in str(card)
