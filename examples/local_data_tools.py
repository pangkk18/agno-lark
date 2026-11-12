"""Register read-only Lark/Feishu data tools on a local Agno Agent."""

from agno.agent import Agent

from agno_lark import LarkBaseTools, LarkSheetsTools, build_lark_data_client, load_settings


def build_agent() -> Agent:
    settings = load_settings()
    data = build_lark_data_client(settings)
    return Agent(
        name="lark-data-reader",
        tools=[LarkSheetsTools(data), LarkBaseTools(data)],
        instructions=["Only read data; never modify a Sheet or Base resource."],
    )

