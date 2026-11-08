"""Construction of Agno remote execution targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agno.agent.remote import RemoteAgent
from agno.team.remote import RemoteTeam
from agno.workflow.remote import RemoteWorkflow

from .config import LarkSettings

TargetType = Literal["agent", "team", "workflow"]


@dataclass(frozen=True)
class TargetDescriptor:
    target_type: TargetType
    target_id: str


def target_descriptor(settings: LarkSettings) -> TargetDescriptor:
    return TargetDescriptor(settings.target_type, settings.target_id)


def build_remote_target(settings: LarkSettings) -> Any:
    base_url = str(settings.agentos_url).rstrip("/")
    if settings.target_type == "agent":
        return RemoteAgent(base_url=base_url, agent_id=settings.target_id)
    if settings.target_type == "team":
        return RemoteTeam(base_url=base_url, team_id=settings.target_id)
    if settings.target_type == "workflow":
        return RemoteWorkflow(base_url=base_url, workflow_id=settings.target_id)
    raise ValueError(f"Unsupported target type: {settings.target_type}")
