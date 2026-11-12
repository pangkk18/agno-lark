# Development Guidelines

This repository uses Python 3.12+, uv, and a `src/` layout. The project is distributed as `agno-lark` and imported as `agno_lark`.

## Common commands

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv build
uv run agno-lark --help
```

Implementation code lives in `src/agno_lark/`, tests live in `tests/`, and the remote AgentOS example lives in `examples/`.

## Code and tests

- Use asynchronous APIs throughout the bridge. The channel integration uses the official `lark-channel-sdk` and its `FeishuChannel` implementation.
- A custom AgentOS interface must inherit from `BaseInterface` and implement `get_router()`.
- Do not log the App Secret, AgentOS token, raw file contents, or complete user messages.
- Add unit tests that use fake Lark/Feishu channels and AgentOS targets before implementing new behavior. Tests must not contact real services.
- Run `uv lock` after dependency changes and keep `pyproject.toml` and `uv.lock` synchronized.
- Run Ruff, mypy, and pytest before submitting changes.

## Compatibility notes

Agno remote targets support `agent`, `team`, and `workflow`. Cancellation must preserve `session_id` and call the corresponding scoped AgentOS cancel endpoint; do not rely on a higher-level method that omits the session.

The first release supports WebSocket long connections, text/image/file messages, and foreground SSE streaming. Group session behavior is controlled by `FEISHU_GROUP_SESSION_MODE`: `user` (the default) isolates context per member, while `shared` shares context per chat/thread. Update the session tests and both README files when changing this mapping.

Read-only Sheets, Base/Bitable, Wiki, and Lingo access lives in `agno_lark.data` and uses the bot tenant token. Keep `/sheet`, `/base`, `/wiki`, and `/lingo` bounded and read-only. Wiki links must resolve to a supported underlying `docx`, `sheet`, or `bitable` resource; do not scrape the UI or use private APIs. Lingo category URL IDs are not a stable public API contract: resolve them against repositories and classifications visible to the application, and fail safely instead of falling back to an unscoped search. HITL requirements are persisted in SQLite and resumed through the target-specific AgentOS continuation API; preserve the original requester, scope, session ID, and authorization for card callbacks and text commands.

The `FEISHU_*` environment variable names and `/feishu/status` route are compatibility surfaces. The canonical public names are `agno-lark`, `agno_lark`, `LarkInterface`, `LarkBridge`, and `LarkSettings`; `FeishuInterface`, `FeishuBridge`, and `FeishuSettings` remain aliases for existing integrations.
