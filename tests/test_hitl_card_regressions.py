"""Offline regressions for callbacks from persisted HITL cards."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from agno_lark.bridge import LarkBridge
from agno_lark.cards import run_card
from agno_lark.state import RunRecord, SqliteStateStore


class Channel:
    def __init__(self):
        self.sent = []
        self.updated = []
        self.streamed = []

    async def send(self, to, envelope):
        self.sent.append((to, envelope))

    async def update_card(self, message_id, card):
        self.updated.append((message_id, card))

    async def stream(self, to, spec):
        self.streamed.append((to, spec))
        await spec["card"]["producer"](SimpleNamespace(update=self._stream_update))

    async def _stream_update(self, card):
        pass


class Target:
    def __init__(self):
        self.calls = []

    async def acontinue_run(self, run_id, requirements, **kwargs):
        self.calls.append((run_id, requirements, kwargs))

        async def events():
            yield SimpleNamespace(event="RunContent", content="continued")
            yield SimpleNamespace(event="RunCompleted", content="continued")

        return events()


def callback(action, run_id="run-1", requirement_id=None, actor="ou-1", chat="oc-1"):
    return SimpleNamespace(
        chat_id=chat,
        message_id="original-card",
        operator=SimpleNamespace(open_id=actor, is_bot=False),
        action=SimpleNamespace(
            value={
                "action": action,
                "run_id": run_id,
                "requirement_id": requirement_id,
            }
        ),
    )


@pytest.fixture
async def paused(tmp_path: Path):
    store = SqliteStateStore(tmp_path / "state.sqlite3")
    channel, target = Channel(), Target()
    settings = SimpleNamespace(
        target_type="agent",
        target_id="assistant",
        agentos_token="test-token",
        group_session_mode="user",
    )
    bridge = LarkBridge(target=target, settings=settings, store=store, channel=channel)
    requirement = RunRequirement(ToolExecution(tool_name="send", requires_confirmation=True))
    try:
        yield bridge, store, channel, target, requirement
    finally:
        await store.close()


async def save_paused(bridge, store, requirement, *, mode="user", thread=None):
    bridge.settings.group_session_mode = mode
    msg = SimpleNamespace(
        sender=SimpleNamespace(open_id="ou-1"),
        conversation=SimpleNamespace(chat_id="oc-1", chat_type="group", thread_id=thread),
    )
    scope, session = await bridge._scope(msg)
    await store.record_run(
        RunRecord(
            scope,
            "run-1",
            session,
            "agent",
            "assistant",
            "oc-1",
            thread,
            status="paused",
        )
    )
    await store.save_hitl_requirement(
        requirement,
        scope_key=scope,
        run_id="run-1",
        requester_user_id="ou-1",
        chat_id="oc-1",
        thread_id=thread,
        session_id=session,
        target_type="agent",
        target_id="assistant",
    )
    return scope, session


@pytest.mark.parametrize("mode", ["user", "shared"])
@pytest.mark.parametrize("thread", [None, "thread-1"])
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_group_callback_resumes_original_scope_and_requester(paused, mode, thread, action):
    bridge, store, channel, target, requirement = paused
    scope, session = await save_paused(bridge, store, requirement, mode=mode, thread=thread)
    await bridge.handle_card_action(callback(f"hitl:{action}", requirement_id=requirement.id))
    assert len(target.calls) == 1
    run_id, requirements, kwargs = target.calls[0]
    assert run_id == "run-1"
    assert requirements[0].confirmation is (action == "approve")
    assert kwargs["user_id"] == "ou-1"
    assert kwargs["session_id"] == session
    assert kwargs["auth_token"] == "test-token"
    assert (await store.list_hitl_requirements(scope))[0]["status"] == "resolved"
    assert (await store.list_runs(scope))[0]["status"] == "completed"


async def test_callback_updates_original_card_instead_of_creating_new_stream(paused):
    bridge, store, channel, target, requirement = paused
    # Shared root scope isolates the original-card bug from the group-scope bug.
    await save_paused(bridge, store, requirement, mode="shared")
    await bridge.handle_card_action(callback("hitl:approve", requirement_id=requirement.id))
    assert target.calls
    assert channel.updated
    assert {mid for mid, _ in channel.updated} == {"original-card"}
    assert "状态：运行中" in str(channel.updated[0][1])
    assert "状态：已完成" in str(channel.updated[-1][1])
    assert all(element["tag"] != "button" for element in channel.updated[-1][1]["body"]["elements"])
    assert not channel.streamed
    assert not channel.sent


@pytest.mark.parametrize("mode", ["user", "shared"])
async def test_status_uses_card_run_id_and_persisted_paused_status(paused, mode):
    bridge, store, channel, target, requirement = paused
    scope, session = await save_paused(bridge, store, requirement, mode=mode)
    await store.record_run(
        RunRecord(
            scope,
            "newer-run",
            session,
            "agent",
            "assistant",
            "oc-1",
            None,
            status="completed",
        )
    )
    # Fresh bridge simulates a restart, with no in-memory active run.
    restarted = LarkBridge(
        target=target,
        settings=bridge.settings,
        store=store,
        channel=channel,
    )
    await restarted.handle_card_action(callback("status"))
    cards = [card for _, card in channel.updated] + [item["card"] for _, item in channel.sent]
    assert "状态：已暂停" in str(cards[-1])
    assert "运行 ID：run-1" in str(cards[-1])
    assert "newer-run" not in str(cards[-1])


def test_status_button_carries_run_id():
    card = run_card(status="paused", run_id="run-1")
    button = next(
        e for e in card["body"]["elements"] if e.get("value", {}).get("action") == "status"
    )
    assert button["value"]["run_id"] == "run-1"


async def test_missing_run_is_not_reported_as_completed(paused):
    bridge, _, channel, _, _ = paused
    await bridge.handle_card_action(callback("status", run_id="missing"))
    cards = [card for _, card in channel.updated] + [item["card"] for _, item in channel.sent]
    assert "已完成" not in str(cards[-1])
    assert "未找到" in str(cards[-1])


@pytest.mark.parametrize("mode", ["user", "shared"])
async def test_other_operator_cannot_resolve_original_request(paused, mode):
    bridge, store, channel, target, requirement = paused
    scope, _ = await save_paused(bridge, store, requirement, mode=mode)
    await bridge.handle_card_action(
        callback("hitl:approve", requirement_id=requirement.id, actor="other")
    )
    assert not target.calls
    assert not channel.updated
    assert (await store.list_hitl_requirements(scope))[0]["status"] == "pending"


@pytest.mark.parametrize(
    "change", [{"chat": "other-chat"}, {"run_id": "missing"}, {"requirement_id": "missing"}]
)
async def test_mismatched_card_does_not_resolve_other_pending_requirement(paused, change):
    bridge, store, _, target, requirement = paused
    scope, _ = await save_paused(bridge, store, requirement, mode="shared")
    args = dict(requirement_id=requirement.id)
    args.update(change)
    await bridge.handle_card_action(callback("hitl:approve", **args))
    assert not target.calls
    assert (await store.list_hitl_requirements(scope))[0]["status"] == "pending"


async def test_concurrent_duplicate_callbacks_continue_only_once(paused):
    bridge, store, _, target, requirement = paused
    await save_paused(bridge, store, requirement, mode="shared")
    event = callback("hitl:approve", requirement_id=requirement.id)
    await asyncio.gather(bridge.handle_card_action(event), bridge.handle_card_action(event))
    assert len(target.calls) == 1


@pytest.mark.parametrize(
    "status,title",
    [
        ("completed", "已完成"),
        ("failed", "失败"),
        ("cancelled", "已取消"),
        ("continuing", "继续执行中"),
        ("cancel_requested", "取消请求中"),
    ],
)
async def test_card_status_preserves_persisted_state(paused, status, title):
    bridge, store, channel, _, requirement = paused
    scope, _ = await save_paused(bridge, store, requirement)
    await store.update_run_by_id(scope, "run-1", status=status)
    await bridge.handle_card_action(callback("status"))
    assert f"状态：{title}" in str(channel.sent[-1][1])
    assert "运行 ID：run-1" in str(channel.sent[-1][1])


@pytest.mark.parametrize("failure", ["exception", "result"])
async def test_card_update_failure_does_not_execute_approval(paused, failure):
    bridge, store, channel, target, requirement = paused
    scope, _ = await save_paused(bridge, store, requirement)

    async def fail_update(_message_id, _card):
        if failure == "exception":
            raise RuntimeError("patch failed")
        return SimpleNamespace(success=False)

    channel.update_card = fail_update
    await bridge.handle_card_action(callback("hitl:approve", requirement_id=requirement.id))
    assert not target.calls
    assert (await store.list_runs(scope))[0]["status"] == "paused"
    assert (await store.list_hitl_requirements(scope))[0]["status"] == "pending"
    assert "尚未继续执行" in str(channel.sent[-1][1])
    assert bridge.last_error


async def test_continuation_exception_updates_original_card_and_persisted_status(paused):
    bridge, store, channel, _, requirement = paused
    scope, _ = await save_paused(bridge, store, requirement)

    class FailingTarget:
        async def acontinue_run(self, run_id, requirements, **kwargs):
            async def events():
                yield SimpleNamespace(event="RunContent", content="partial")
                raise RuntimeError("continuation failed")

            return events()

    bridge.target = FailingTarget()
    await bridge.handle_card_action(callback("hitl:approve", requirement_id=requirement.id))
    assert "状态：失败" in str(channel.updated[-1][1])
    assert (await store.list_runs(scope))[0]["status"] == "failed"
    assert not channel.streamed
    assert not channel.sent


async def test_continuation_repauses_original_card_with_new_requirement(paused):
    bridge, store, channel, _, requirement = paused
    scope, _ = await save_paused(bridge, store, requirement)
    next_requirement = RunRequirement(ToolExecution(tool_name="next", requires_confirmation=True))

    class PausingTarget:
        async def acontinue_run(self, run_id, requirements, **kwargs):
            async def events():
                yield SimpleNamespace(event="RunPaused", requirements=[next_requirement])

            return events()

    bridge.target = PausingTarget()
    await bridge.handle_card_action(callback("hitl:approve", requirement_id=requirement.id))
    card = channel.updated[-1][1]
    assert "状态：已暂停" in str(card)
    approvals = [
        e["value"]
        for e in card["body"]["elements"]
        if e.get("value", {}).get("action") == "hitl:approve"
    ]
    assert approvals == [
        {"action": "hitl:approve", "run_id": "run-1", "requirement_id": next_requirement.id}
    ]
    assert (await store.list_runs(scope))[0]["status"] == "paused"
    assert (await store.get_hitl_requirement(scope, "run-1", requirement.id))[
        "status"
    ] == "resolved"
    assert (await store.get_hitl_requirement(scope, "run-1", next_requirement.id))[
        "status"
    ] == "pending"


async def test_normal_group_message_and_callback_use_same_persisted_scope(paused):
    bridge, store, channel, target, requirement = paused

    async def arun(_text, **kwargs):
        async def events():
            yield SimpleNamespace(event="RunStarted", run_id="run-1")
            yield SimpleNamespace(event="RunPaused", run_id="run-1", requirements=[requirement])

        return events()

    target.arun = arun
    msg = SimpleNamespace(
        id="message-1",
        body_text="please send",
        mentioned_bot=True,
        sender=SimpleNamespace(open_id="ou-1", is_bot=False),
        conversation=SimpleNamespace(chat_id="oc-1", chat_type="group", thread_id="thread-1"),
    )
    await bridge.handle_message(msg)
    scope, session = await bridge._scope(msg)
    await bridge.active[scope].task
    assert (await store.list_runs(scope))[0]["status"] == "paused"
    assert len(channel.streamed) == 1
    await bridge.handle_card_action(callback("hitl:approve", requirement_id=requirement.id))
    assert len(target.calls) == 1
    assert target.calls[0][2]["session_id"] == session
    assert len(channel.streamed) == 1
    assert "状态：已完成" in str(channel.updated[-1][1])


async def test_run_lookup_is_scoped_and_rejects_ambiguous_card_ids(paused):
    bridge, store, _, _, requirement = paused
    scope, _ = await save_paused(bridge, store, requirement)
    assert await store.get_run("other-scope", "run-1") is None
    assert (await store.get_run(scope, "run-1"))["status"] == "paused"
    lookup = dict(chat_id="oc-1", target_type="agent", target_id="assistant")
    assert await store.find_card_run("run-1", **{**lookup, "target_id": "other"}) is None
    await store.record_run(
        RunRecord(
            "other-scope",
            "run-1",
            "other-session",
            "agent",
            "assistant",
            "oc-1",
            None,
        )
    )
    assert await store.find_card_run("run-1", **lookup) is None
