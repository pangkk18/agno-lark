"""Agno AgentOS interface for the Lark/Feishu bridge."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, cast

from agno.os.interfaces.base import BaseInterface
from fastapi import APIRouter, FastAPI

from .bridge import LarkBridge


class LarkInterface(BaseInterface):
    type = "feishu"
    version = "1.0"
    prefix = "/feishu"
    tags = ["feishu"]
    authenticates_own_requests = True

    def __init__(
        self,
        *,
        target: Any,
        settings: Any,
        store: Any | None = None,
        channel: Any | None = None,
        prefix: str = "/feishu",
    ) -> None:
        self.target, self.settings, self.store = target, settings, store
        self.prefix = prefix
        target_type = getattr(settings, "target_type", "agent")
        self.agent = target if target_type == "agent" else None
        self.team = target if target_type == "team" else None
        self.workflow = target if target_type == "workflow" else None
        if channel is None:
            from lark_channel import (  # type: ignore[import-untyped]
                DedupConfig,
                FeishuChannel,
                PolicyConfig,
                SafetyConfig,
                SecurityConfig,
            )

            allowed_users = sorted(getattr(settings, "allowed_user_ids", set()) or set())
            allowed_chats = sorted(getattr(settings, "allowed_chat_ids", set()) or set())
            policy = PolicyConfig(
                require_mention=True,
                dm_policy="allowlist" if allowed_users else "open",
                group_policy="allowlist" if allowed_chats else "open",
                allow_from=allowed_users or None,
                group_allowlist=allowed_chats or None,
            )

            secret = settings.feishu_app_secret
            get_secret = getattr(secret, "get_secret_value", None)
            channel = FeishuChannel(
                app_id=settings.feishu_app_id,
                app_secret=get_secret() if callable(get_secret) else str(secret),
                policy=policy,
                safety=SafetyConfig(dedup=DedupConfig(enabled=True, ttl_seconds=86400)),
                security=SecurityConfig(mode="strict", allow_local_insecure_ws=True),
            )
        self.channel = channel
        self.bridge = LarkBridge(target=target, settings=settings, store=store, channel=channel)
        self.router = self.get_router()

    def get_scope_mappings(self) -> dict[str, list[str]]:
        return {f"GET {self.prefix}/status": []}

    def get_router(self, use_async: bool = True, **kwargs: Any) -> APIRouter:
        if not use_async:
            raise ValueError("LarkInterface only supports async AgentOS execution")

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            if self.store is not None and hasattr(self.store, "open"):
                await self.store.open()
            try:
                await self.bridge.start()
                yield
            finally:
                await self.bridge.stop()
                if self.store is not None and hasattr(self.store, "close"):
                    await self.store.close()

        router = APIRouter(
            prefix=self.prefix, tags=cast(list[str | Enum], self.tags), lifespan=lifespan
        )

        @router.get("/status")
        async def status() -> dict[str, Any]:
            return self.bridge.status()

        return router


# Backward-compatible name for integrations that used the original Feishu branding.
FeishuInterface = LarkInterface

__all__ = ["LarkInterface", "FeishuInterface"]
