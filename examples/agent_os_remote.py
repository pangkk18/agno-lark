"""Run a remote AgentOS target with the Lark/Feishu interface."""

from agno.os import AgentOS

from agno_lark import LarkInterface, build_remote_target, load_settings


def create_gateway() -> AgentOS:
    """Create an AgentOS gateway from environment configuration."""

    settings = load_settings()
    target = build_remote_target(settings)
    interface = LarkInterface(target=target, settings=settings)
    return AgentOS(
        agents=[target] if settings.target_type == "agent" else None,
        teams=[target] if settings.target_type == "team" else None,
        workflows=[target] if settings.target_type == "workflow" else None,
        interfaces=[interface],
    )


if __name__ == "__main__":
    create_gateway().serve(app="examples.agent_os_remote:create_gateway")
