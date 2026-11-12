from __future__ import annotations

from types import SimpleNamespace

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from agno_lark.bridge import LarkBridge


class Channel:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, _to: str, envelope: dict) -> None:
        self.sent.append(envelope)


class DataClient:
    async def read_sheet_values(self, _token: str, _range: str):
        return SimpleNamespace(values=[["name"], ["Ada"]], truncated=False)


class HitlStore:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.updated = False

    async def list_hitl_requirements(self, _scope: str):
        return [
            {
                "run_id": "run-1",
                "requirement_id": self.payload["id"],
                "payload": self.payload,
                "status": "pending",
                "requester_user_id": "ou-1",
                "session_id": "session-1",
                "target_type": "agent",
            }
        ]

    async def update_hitl_requirement(self, *_args, **_kwargs) -> None:
        self.updated = True


class Target:
    def __init__(self) -> None:
        self.confirmed = False

    async def acontinue_run(self, run_id, requirements, **_kwargs):
        assert run_id == "run-1"
        self.confirmed = requirements[0].confirmation is True

        async def events():
            yield SimpleNamespace(event="RunCompleted", content="done")

        return events()


def message() -> SimpleNamespace:
    return SimpleNamespace(
        sender=SimpleNamespace(open_id="ou-1", is_bot=False),
        conversation=SimpleNamespace(chat_id="oc-1", thread_id=None),
    )


def settings() -> SimpleNamespace:
    return SimpleNamespace(target_type="agent", target_id="assistant", agentos_token=None)


@pytest.mark.asyncio
async def test_sheet_command_uses_read_only_data_client() -> None:
    channel = Channel()
    bridge = LarkBridge(
        target=object(),
        settings=settings(),
        store=SimpleNamespace(),
        channel=channel,
        data_client=DataClient(),
    )
    msg = message()
    await bridge.handle_command("/sheet shtabc Sheet1!A1:A2", msg, "scope", "session")
    assert "Ada" in str(channel.sent[-1])


@pytest.mark.asyncio
async def test_hitl_approve_continues_original_requester() -> None:
    requirement = RunRequirement(
        ToolExecution(tool_name="send", requires_confirmation=True)
    )
    target = Target()
    store = HitlStore(requirement.to_dict())
    bridge = LarkBridge(target=target, settings=settings(), store=store, channel=Channel())
    await bridge.handle_command("/approve", message(), "scope", "session")
    assert target.confirmed is True
    assert store.updated is True
