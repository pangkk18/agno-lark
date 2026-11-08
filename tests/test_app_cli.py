from __future__ import annotations

from pathlib import Path

from agno_lark import FeishuInterface, LarkInterface
from agno_lark.app import create_app
from agno_lark.cli import build_parser
from agno_lark.config import FeishuSettings, LarkSettings


def test_cli_parser_accepts_bind_overrides() -> None:
    args = build_parser().parse_args(["--host", "0.0.0.0", "--port", "9000", "--reload"])
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.reload is True
    assert build_parser().prog == "agno-lark"


def test_lark_names_are_canonical_with_feishu_compatibility_aliases() -> None:
    assert FeishuInterface is LarkInterface
    assert FeishuSettings is LarkSettings


def test_create_app_does_not_require_remote_agentos(tmp_path: Path) -> None:
    settings = LarkSettings(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        agentos_url="http://127.0.0.1:7777",
        target_id="assistant",
        state_db=tmp_path / "state.sqlite3",
    )
    app = create_app(settings)
    assert "/feishu/status" in app.openapi()["paths"]
