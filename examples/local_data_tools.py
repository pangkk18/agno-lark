"""Register read-only Lark/Feishu data tools on a local Agno Agent."""

from agno.agent import Agent

from agno_lark import LarkDataTools, build_lark_data_client, load_settings


def build_agent() -> Agent:
    settings = load_settings()
    data = build_lark_data_client(settings)
    return Agent(
        name="lark-data-reader",
        tools=[LarkDataTools(data)],
        instructions=[
            "Read Lark/Feishu Sheets, Base, Wiki, and Lingo resources when asked.",
            "Only read data; never modify a resource.",
            "Do not guess a Lingo repository or classification when a "
            "category URL cannot be resolved.",
        ],
    )

