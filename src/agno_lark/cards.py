"""Small, dependency-free Lark/Feishu card builders."""

from __future__ import annotations

from typing import Any


def run_card(
    *,
    status: str,
    output: str = "",
    run_id: str | None = None,
    elapsed: float | None = None,
    requirement_ids: list[str] | None = None,
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
    if status in {"running", "paused"}:
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
        if status == "paused":
            requirement_ids = requirement_ids or [""]
            elements.extend(
                [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准"},
                        "type": "primary",
                        "value": {
                            "action": "hitl:approve",
                            "run_id": run_id or "",
                            "requirement_id": requirement_ids[0],
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "hitl:reject",
                            "run_id": run_id or "",
                            "requirement_id": requirement_ids[0],
                        },
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
        output=(
            "/new 重置会话\n/status 查看运行\n/cancel 取消运行\n"
            "/sheet <token> <Sheet!A1:D20> 读取表格\n"
            "/base <token> <table_id> [关键词] 读取多维表格\n"
            "/approve、/reject、/input、/feedback、/external 处理 HITL\n/help 查看帮助"
        ),
    )


def data_card(
    *,
    title: str,
    headers: list[str],
    rows: list[list[Any]],
    truncated: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    """Render bounded Sheet/Base data without echoing resource credentials."""
    if error:
        content = f"**{title}**\n\n读取失败：{error}"
    else:
        lines = [f"**{title}**"]
        if headers:
            lines.append(" | ".join(str(item) for item in headers))
            lines.append(" | ".join("---" for _ in headers))
        for row in rows:
            values = [str(item).replace("|", "\\|").replace("\n", " ") for item in row]
            lines.append(" | ".join(values))
        if truncated:
            lines.append("\n已截断：结果超过桥接器配置的读取上限。")
        content = "\n".join(lines)
    return {
        "schema": "2.0",
        "body": {
            "direction": "vertical",
            "padding": "12px",
            "elements": [{"tag": "markdown", "content": content}],
        },
    }
