"""Application settings for the Lark/Feishu bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class LarkSettings(BaseSettings):
    """Environment-backed bridge configuration."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    feishu_app_id: str
    feishu_app_secret: SecretStr
    agentos_url: AnyHttpUrl
    agentos_token: SecretStr | None = None
    target_type: Literal["agent", "team", "workflow"] = Field(
        default="agent",
        validation_alias=AliasChoices("AGENTOS_TARGET_TYPE", "target_type"),
    )
    target_id: str = Field(
        default="",
        validation_alias=AliasChoices("AGENTOS_TARGET_ID", "AGENTOS_AGENT_ID", "target_id"),
    )
    group_session_mode: Literal["user", "shared"] = Field(
        default="user",
        validation_alias=AliasChoices("FEISHU_GROUP_SESSION_MODE", "group_session_mode"),
    )
    state_db: Path = Field(
        default=Path(".data/agno-feishu.sqlite3"),
        validation_alias=AliasChoices("FEISHU_STATE_DB", "state_db"),
    )
    allowed_user_ids: Annotated[set[str], NoDecode] = Field(
        default_factory=set,
        validation_alias=AliasChoices("FEISHU_ALLOWED_USER_IDS", "allowed_user_ids"),
    )
    allowed_chat_ids: Annotated[set[str], NoDecode] = Field(
        default_factory=set,
        validation_alias=AliasChoices("FEISHU_ALLOWED_CHAT_IDS", "allowed_chat_ids"),
    )
    bridge_host: str = Field(
        default="127.0.0.1", validation_alias=AliasChoices("BRIDGE_HOST", "bridge_host")
    )
    bridge_port: int = Field(
        default=7788, validation_alias=AliasChoices("BRIDGE_PORT", "bridge_port")
    )
    lark_api_base_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("https://open.feishu.cn"),
        validation_alias=AliasChoices("LARK_API_BASE_URL", "lark_api_base_url"),
    )
    lark_api_timeout: float = Field(
        default=20.0, validation_alias=AliasChoices("LARK_API_TIMEOUT", "lark_api_timeout")
    )
    lark_data_max_rows: int = Field(
        default=100, validation_alias=AliasChoices("LARK_DATA_MAX_ROWS", "lark_data_max_rows")
    )
    lark_data_max_cells: int = Field(
        default=2000, validation_alias=AliasChoices("LARK_DATA_MAX_CELLS", "lark_data_max_cells")
    )
    lark_base_page_size: int = Field(
        default=50, validation_alias=AliasChoices("LARK_BASE_PAGE_SIZE", "lark_base_page_size")
    )

    @field_validator("target_id", mode="before")
    @classmethod
    def target_alias(cls, value: Any) -> str:
        return str(value or "")

    @field_validator("agentos_token", mode="before")
    @classmethod
    def blank_token_is_none(cls, value: Any) -> Any:
        return None if value in (None, "") else value

    @field_validator("allowed_user_ids", "allowed_chat_ids", mode="before")
    @classmethod
    def parse_ids(cls, value: Any) -> set[str]:
        if value is None or value == "":
            return set()
        if isinstance(value, str):
            return {item.strip() for item in value.split(",") if item.strip()}
        return {str(item).strip() for item in value if str(item).strip()}

    @field_validator(
        "lark_api_timeout", "lark_data_max_rows", "lark_data_max_cells", "lark_base_page_size"
    )
    @classmethod
    def positive_data_limits(cls, value: Any) -> Any:
        if float(value) <= 0:
            raise ValueError("Lark data limits and timeout must be positive")
        return value

    def model_post_init(self, __context: Any) -> None:
        if not self.target_id:
            import os

            legacy = os.getenv("AGENTOS_AGENT_ID")
            if legacy:
                object.__setattr__(self, "target_id", legacy)
        if not self.target_id:
            raise ValueError("AGENTOS_TARGET_ID (or legacy AGENTOS_AGENT_ID) is required")


FeishuSettings = LarkSettings


def load_settings() -> LarkSettings:
    return LarkSettings()  # type: ignore[call-arg]


__all__ = ["LarkSettings", "FeishuSettings", "load_settings"]
