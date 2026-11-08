from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import FastAPI

from agno_lark.bridge import ActiveRun, LarkBridge
from agno_lark.cards import run_card
from agno_lark.interface import LarkInterface


class FakeChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.sent: list[tuple[str, dict]] = []
        self.connected = False

    def on(self, name: str, handler: object):
        self.handlers[name] = handler
        return lambda: self.handlers.pop(name, None)

    async def connect_until_ready(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send(self, to: str, card: dict) -> None:
        self.sent.append((to, card))


class FakeTarget:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def arun(self, text: str, **kwargs):
        self.calls.append({"text": text, **kwargs})

        async def events():
            yield SimpleNamespace(event="RunStarted", run_id="run-1")
            yield SimpleNamespace(event="RunContent", content="hello")
            yield SimpleNamespace(event="RunCompleted", content="hello")

        return events()


class ErrorTarget:
    async def arun(self, text: str, **kwargs):
        async def events():
            yield SimpleNamespace(event="RunStarted", run_id="run-error")
            yield SimpleNamespace(event="RunError", run_id="run-error", error_type="provider_error")

        return events()


class MemoryStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.opened = False
        self.closed = False
        self.updates: list[tuple[tuple, dict]] = []

    async def seen_message(self, message_id: str) -> bool:
        already = message_id in self.seen
        self.seen.add(message_id)
        return already

    async def get_generation(self, _scope: str) -> int:
        return 0

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def update_run(self, *args, **kwargs) -> None:
        self.updates.append((args, kwargs))


def message(text: str = "hi", *, mentioned: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"m-{text}",
        body_text=text,
        content_text=text,
        mentioned_bot=mentioned,
        sender=SimpleNamespace(open_id="ou_1", is_bot=False),
        conversation=SimpleNamespace(chat_id="oc_1", chat_type="p2p", thread_id=None),
        resources=[],
    )


def settings(**overrides):
    values = dict(
        target_type="agent",
        target_id="assistant",
        agentos_url="https://agent.example.test",
        agentos_token=None,
        allowed_user_ids=set(),
        allowed_chat_ids=set(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_message_is_deduplicated_and_forwarded() -> None:
    channel = FakeChannel()
    target = FakeTarget()
    store = MemoryStore()
    bridge = LarkBridge(target=target, settings=settings(), store=store, channel=channel)
    msg = message()
    await bridge.handle_message(msg)
    await bridge.handle_message(msg)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(target.calls) == 1
    assert target.calls[0]["session_id"].startswith("feishu:agent:assistant:oc_1:root")
    assert target.calls[0]["add_history_to_context"] is True
    assert any("hello" in str(card) for _, card in channel.sent)


@pytest.mark.asyncio
async def test_agentos_error_event_is_not_marked_completed() -> None:
    channel = FakeChannel()
    store = MemoryStore()
    bridge = LarkBridge(target=ErrorTarget(), settings=settings(), store=store, channel=channel)
    await bridge.handle_message(message("error"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any("状态：失败" in str(card) for _, card in channel.sent)
    assert store.updates[-1][1]["status"] == "failed"
    assert store.updates[-1][1]["error"] == "provider_error"


def test_running_card_uses_schema_v2_button_elements() -> None:
    card = run_card(status="running", run_id="run-1")
    elements = card["body"]["elements"]
    assert [element["tag"] for element in elements] == ["markdown", "button", "button"]
    assert all(element["tag"] != "action" for element in elements)


@pytest.mark.asyncio
async def test_send_wraps_card_for_channel_sdk_coercion() -> None:
    channel = FakeChannel()
    bridge = LarkBridge(
        target=FakeTarget(), settings=settings(), store=MemoryStore(), channel=channel
    )
    await bridge._send(message(), run_card(status="completed", output="ok"))
    assert channel.sent[-1][1]["card"]["schema"] == "2.0"


@pytest.mark.asyncio
async def test_group_requires_mention_and_allowlist_is_applied() -> None:
    channel = FakeChannel()
    target = FakeTarget()
    store = MemoryStore()
    bridge = LarkBridge(
        target=target,
        settings=settings(allowed_user_ids={"ou_allowed"}),
        store=store,
        channel=channel,
    )
    group = message("hello", mentioned=False)
    group.conversation.chat_type = "group"
    await bridge.handle_message(group)
    assert not target.calls


@pytest.mark.asyncio
async def test_group_scope_defaults_to_per_user_and_supports_shared_mode() -> None:
    channel = FakeChannel()
    store = MemoryStore()
    first = message("one")
    first.conversation.chat_type = "group"
    second = message("two")
    second.conversation.chat_type = "group"
    second.sender.open_id = "ou_2"

    per_user = LarkBridge(target=FakeTarget(), settings=settings(), store=store, channel=channel)
    first_scope, _ = await per_user._scope(first)
    second_scope, _ = await per_user._scope(second)
    assert first_scope != second_scope

    shared = LarkBridge(
        target=FakeTarget(),
        settings=settings(group_session_mode="shared"),
        store=store,
        channel=channel,
    )
    shared_first, _ = await shared._scope(first)
    shared_second, _ = await shared._scope(second)
    assert shared_first == shared_second


@pytest.mark.asyncio
@respx.mock
async def test_cancel_uses_scoped_endpoint_and_bearer_token() -> None:
    route = respx.post(
        "https://agent.example.test/agents/assistant/runs/run-1/cancel",
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))
    channel = FakeChannel()
    bridge = LarkBridge(
        target=FakeTarget(),
        settings=settings(agentos_token="secret"),
        store=MemoryStore(),
        channel=channel,
    )
    task = asyncio.create_task(asyncio.sleep(10))
    run = ActiveRun(task, "scope", "session-3", "run-1", 1.0)
    await bridge._cancel_remote(run)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert route.called, bridge.last_error
    assert route.calls[0].request.url.params["session_id"] == "session-3"
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
@respx.mock
async def test_cancel_falls_back_when_socks_proxy_support_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in proxy_names:
        monkeypatch.setenv(name, "socks5://127.0.0.1:9999")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    route = respx.post(
        "https://agent.example.test/agents/assistant/runs/run-1/cancel",
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))
    bridge = LarkBridge(
        target=FakeTarget(),
        settings=settings(agentos_token="secret"),
        store=MemoryStore(),
        channel=FakeChannel(),
    )
    task = asyncio.create_task(asyncio.sleep(10))
    run = ActiveRun(task, "scope", "session-3", "run-1", 1.0)
    await bridge._cancel_remote(run)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert route.called, bridge.last_error


@pytest.mark.asyncio
async def test_interface_lifespan_connects_and_closes() -> None:
    channel = FakeChannel()
    target = FakeTarget()
    store = MemoryStore()
    interface = LarkInterface(target=target, settings=settings(), store=store, channel=channel)
    app = FastAPI()
    async with interface.router.lifespan_context(app):
        assert channel.connected is True
        assert store.opened is True
    assert channel.connected is False
    assert store.closed is True
    with pytest.raises(ValueError, match="async"):
        interface.get_router(use_async=False)


@pytest.mark.asyncio
async def test_card_action_uses_operator_and_chat_allowlist() -> None:
    channel = FakeChannel()
    store = MemoryStore()
    bridge = LarkBridge(
        target=FakeTarget(),
        settings=settings(allowed_user_ids={"ou_1"}, allowed_chat_ids={"oc_1"}),
        store=store,
        channel=channel,
    )
    event = SimpleNamespace(
        chat_id="oc_1",
        message_id="card-1",
        operator=SimpleNamespace(open_id="ou_1", is_bot=False),
        action=SimpleNamespace(value={"action": "status"}),
    )
    await bridge.handle_card_action(event)
    assert channel.sent
