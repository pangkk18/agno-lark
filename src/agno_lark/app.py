"""Application factory for the Agno AgentOS Lark/Feishu bridge."""

from __future__ import annotations

from typing import Any

from agno.agent.remote import RemoteAgent
from agno.os import AgentOS
from agno.team.remote import RemoteTeam
from agno.workflow.remote import RemoteWorkflow
from fastapi import FastAPI

from .config import LarkSettings, load_settings
from .interface import LarkInterface
from .state import SqliteStateStore
from .target import build_remote_target


def create_app(settings: LarkSettings | None = None) -> FastAPI:
    """Build a FastAPI AgentOS gateway configured for Lark/Feishu.

    The returned application owns the interface lifespan.  On startup the
    interface opens the SQLite state store and establishes the Lark/Feishu channel;
    on shutdown it disconnects the channel and closes the store.
    """

    resolved = settings or load_settings()
    target = build_remote_target(resolved)
    store = SqliteStateStore(resolved.state_db)
    interface = LarkInterface(target=target, settings=resolved, store=store)

    target_type = resolved.target_type
    agents: Any = [target] if target_type == "agent" else None
    teams: Any = [target] if target_type == "team" else None
    workflows: Any = [target] if target_type == "workflow" else None

    agent_os = AgentOS(
        agents=agents,
        teams=teams,
        workflows=workflows,
        interfaces=[interface],
    )
    # Agno 3.1 eagerly inspects remote metadata while assembling an app. Temporarily
    # hide the client during assembly so a bridge can start before the upstream is up;
    # restore it immediately for normal ``arun`` calls.
    remote_client = getattr(target, "agentos_client", None)
    if isinstance(target, (RemoteAgent, RemoteTeam, RemoteWorkflow)):
        target.agentos_client = None
    try:
        return agent_os.get_app()
    finally:
        if isinstance(target, (RemoteAgent, RemoteTeam, RemoteWorkflow)):
            target.agentos_client = remote_client


__all__ = ["create_app"]
