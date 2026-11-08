"""Deterministic Lark/Feishu conversation scope and AgentOS session IDs."""

from __future__ import annotations

import hashlib


def make_scope_key(
    target_type: str,
    target_id: str,
    chat_id: str,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> str:
    parts = ["feishu", target_type, target_id, chat_id, thread_id or "root"]
    if user_id:
        parts.extend(("user", user_id))
    return ":".join(parts)


def make_session_id(scope_key: str, generation: int = 0, max_length: int = 240) -> str:
    value = f"{scope_key}:v{generation}"
    if len(value) <= max_length:
        return value
    digest = hashlib.sha256(scope_key.encode()).hexdigest()[:24]
    return f"feishu:{digest}:v{generation}"


def scope_for_message(
    target_type: str,
    target_id: str,
    chat_id: str,
    thread_id: str | None,
    user_id: str | None = None,
) -> str:
    return make_scope_key(target_type, target_id, chat_id, thread_id, user_id)
