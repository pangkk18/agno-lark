from __future__ import annotations

from pathlib import Path

import pytest

from agno_lark.config import LarkSettings
from agno_lark.media import resource_to_media
from agno_lark.session import make_scope_key, make_session_id
from agno_lark.state import RunRecord, SqliteStateStore
from agno_lark.target import build_remote_target


def test_settings_parse_lists_and_legacy_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("AGENTOS_URL", "http://127.0.0.1:7777")
    monkeypatch.setenv("AGENTOS_AGENT_ID", "assistant")
    monkeypatch.setenv("FEISHU_ALLOWED_USER_IDS", "ou_a, ou_b")
    settings = LarkSettings()
    assert settings.target_id == "assistant"
    assert settings.allowed_user_ids == {"ou_a", "ou_b"}
    assert settings.group_session_mode == "user"


def test_group_session_mode_can_use_shared_context() -> None:
    settings = LarkSettings(
        feishu_app_id="x",
        feishu_app_secret="s",
        agentos_url="http://localhost",
        target_id="assistant",
        group_session_mode="shared",
    )
    assert settings.group_session_mode == "shared"


def test_target_factory() -> None:
    common = dict(feishu_app_id="x", feishu_app_secret="s", agentos_url="http://localhost")
    for kind, attr in (("agent", "agent_id"), ("team", "team_id"), ("workflow", "workflow_id")):
        settings = LarkSettings(**common, target_type=kind, target_id="target")
        assert getattr(build_remote_target(settings), attr) == "target"


def test_session_ids_are_stable_and_shortened() -> None:
    scope = make_scope_key("agent", "assistant", "chat", "thread")
    assert make_session_id(scope, 3).endswith(":v3")
    assert make_session_id("x" * 500, 2).startswith("feishu:")
    assert make_scope_key("agent", "assistant", "chat", "thread", "ou_1").endswith(
        ":user:ou_1"
    )


@pytest.mark.asyncio
async def test_sqlite_dedup_generation_and_runs(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "state.sqlite3")
    assert await store.seen_message("m1") is False
    assert await store.seen_message("m1") is True
    assert await store.increment_generation("scope") == 1
    await store.record_run(RunRecord("scope", "run", "session", "agent", "a", "chat", None))
    assert (await store.active_runs("scope"))[0]["run_id"] == "run"
    await store.update_run(
        "scope", (await store.list_runs("scope"))[0]["started_at"], status="completed"
    )
    assert await store.active_runs("scope") == []
    await store.close()


@pytest.mark.asyncio
async def test_media_conversion_and_unsupported_type() -> None:
    class Channel:
        async def download_resource(self, *_args, **_kwargs):
            return b"bytes"

    image_resource = type(
        "Resource", (), {"type": "image", "file_key": "k", "file_name": "a.png"}
    )()
    image = await resource_to_media(
        Channel(), "m1", image_resource
    )
    assert image.__class__.__name__ == "Image"
    with pytest.raises(ValueError, match="Unsupported"):
        await resource_to_media(
            Channel(), "m1", type("Resource", (), {"type": "audio", "file_key": "k"})()
        )
