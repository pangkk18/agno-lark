"""Small, dependency-free Lark/Feishu card builders."""

from __future__ import annotations

from typing import Any


def run_card(
    *, status: str, output: str = "", run_id: str | None = None, elapsed: float | None = None
) -> dict[str, Any]:
    """Return a V2 card suitable for ``FeishuChannel.send`` or ``stream``."""
    title = {
        "running": "运行中",
        "completed": "已完成",
        "cancelled": "已取消",
        "failed": "失败",
        "paused": "已暂停",
    }.get(status, status)
    lines = [f"状态：{title}"]
    if run_id:
        lines.append(f"运行 ID：{run_id}")
    if elapsed is not None:
        lines.append(f"耗时：{elapsed:.1f}s")
    if output:
        lines.append(output)
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n\n".join(lines)}]
    if status == "running":
        # Card schema 2.0 does not support the v1 ``action`` container.  Its
        # interactive buttons are top-level body elements (or children of a
        # ``column_set``), as used by lark-channel's CardKit builder.
        elements.extend(
            [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看状态"},
                    "type": "primary",
                    "value": {"action": "status"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消运行"},
                    "type": "danger",
                    "value": {"action": "cancel", "run_id": run_id or ""},
                },
            ]
        )
    return {
        "schema": "2.0",
        "body": {"direction": "vertical", "padding": "12px 12px 12px 12px", "elements": elements},
    }


def help_card() -> dict[str, Any]:
    return run_card(
        status="completed",
        output="/new 重置会话\n/status 查看运行\n/cancel 取消运行\n/help 查看帮助",
    )
