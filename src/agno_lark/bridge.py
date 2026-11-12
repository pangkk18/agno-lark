"""Lark/Feishu event bridge and AgentOS execution orchestration."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from lark_channel import Events  # type: ignore[import-untyped]

from .cards import data_card, help_card, run_card
from .media import resource_to_media
from .session import make_scope_key, make_session_id
from .state import RunRecord

log = logging.getLogger(__name__)


@dataclass
class ActiveRun:
    task: asyncio.Task[Any]
    scope_key: str
    session_id: str
    run_id: str | None = None
    started_at: float = 0.0
    requester_user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None


class LarkBridge:
    def __init__(
        self,
        *,
        target: Any,
        settings: Any,
        store: Any,
        channel: Any,
        data_client: Any | None = None,
    ) -> None:
        self.target, self.settings, self.store, self.channel = target, settings, store, channel
        self.data_client = data_client
        self.active: dict[str, ActiveRun] = {}
        self.last_error: str | None = None
        # These counters intentionally contain metadata only.  They make the
        # status endpoint useful when a bot is connected but no reply is
        # visible: a message can be absent because the platform did not
        # deliver it, the SDK policy rejected it, or the bridge filtered it.
        self.received_messages = 0
        self.rejected_messages = 0
        self.filtered_messages = 0
        self.last_reject_reason: str | None = None
        self.pending_hitl = 0
        self._unsubs: list[Any] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def register(self) -> None:
        self._unsubs.append(self.channel.on(Events.MESSAGE, self.handle_message))
        self._unsubs.append(self.channel.on(Events.CARD_ACTION, self.handle_card_action))
        # The channel safety pipeline runs before the MESSAGE callback.  Keep
        # its reject signal visible in status so policy/mention/allowlist
        # drops can be distinguished from a missing platform subscription.
        reject_event = getattr(Events, "REJECT", "reject")
        self._unsubs.append(self.channel.on(reject_event, self._handle_reject_event))
        for event in (Events.ERROR, Events.RECONNECTING, Events.RECONNECTED):
            self._unsubs.append(self.channel.on(event, self._handle_channel_event))

    async def start(self) -> None:
        self.register()
        result = self.channel.connect_until_ready()
        if inspect.isawaitable(result):
            await result

    async def stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                log.debug("channel unsubscribe failed", exc_info=True)
        self._unsubs.clear()
        tasks = [run.task for run in self.active.values() if not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        result = self.channel.disconnect()
        if inspect.isawaitable(result):
            await result

    async def _handle_channel_event(self, event: Any) -> None:
        if event is not None and not isinstance(event, str):
            event = getattr(event, "error", None) or getattr(event, "message", None) or str(event)
        if event:
            self.last_error = str(event)

    async def _handle_reject_event(self, event: Any) -> None:
        """Record SDK safety-policy drops without retaining message content."""

        self.rejected_messages += 1
        reason = getattr(event, "reason", None)
        if reason is None and isinstance(event, dict):
            reason = event.get("reason")
        self.last_reject_reason = str(reason) if reason else "unknown"

    def _allowed(self, msg: Any) -> bool:
        sender_obj = getattr(msg, "sender", None) or getattr(msg, "operator", None)
        conversation = getattr(msg, "conversation", None)
        sender = getattr(sender_obj, "open_id", None)
        chat = getattr(conversation, "chat_id", None) or getattr(msg, "chat_id", None)
        if getattr(sender_obj, "is_bot", False):
            return False
        if str(getattr(msg, "raw_content_type", "")).lower() in {"system", "unknown"}:
            return False
        users: set[str] = getattr(self.settings, "allowed_user_ids", set()) or set()
        chats: set[str] = getattr(self.settings, "allowed_chat_ids", set()) or set()
        return (not users or sender in users) and (not chats or chat in chats)

    @staticmethod
    def _text(msg: Any) -> str:
        return str(
            getattr(msg, "body_text", None) or getattr(msg, "content_text", None) or ""
        ).strip()

    @staticmethod
    def _is_group(msg: Any) -> bool:
        chat_type = str(getattr(getattr(msg, "conversation", None), "chat_type", ""))
        return chat_type.lower() in {"group", "chat_type.group"}

    async def handle_message(self, msg: Any) -> None:
        self.received_messages += 1
        if not self._allowed(msg):
            self.filtered_messages += 1
            return
        if not getattr(msg, "id", None):
            self.filtered_messages += 1
            return
        if hasattr(self.store, "seen_message") and await self.store.seen_message(msg.id):
            return
        text = self._text(msg)
        if not text:
            self.filtered_messages += 1
            return
        if self._is_group(msg) and not getattr(msg, "mentioned_bot", False):
            self.filtered_messages += 1
            return
        scope_key, session_id = await self._scope(msg)
        if text.startswith("/"):
            await self.handle_command(text, msg, scope_key, session_id)
            return
        if hasattr(self.store, "list_hitl_requirements"):
            pending = [
                row
                for row in await self.store.list_hitl_requirements(scope_key)
                if row.get("status") == "pending"
            ]
            if len(pending) == 1:
                await self._continue_paused(scope_key, session_id, msg, "/continue", [text])
                return
        lock = self._locks.setdefault(scope_key, asyncio.Lock())
        current = self.active.get(scope_key)
        if lock.locked() or (current is not None and not current.task.done()):
            await self._send(
                msg, run_card(status="completed", output="当前会话已有运行，请稍后再试")
            )
            return
        if not callable(getattr(self.channel, "stream", None)):
            await self._send(msg, run_card(status="running"))
        task = asyncio.create_task(self._run(msg, text, scope_key, session_id))
        conversation = getattr(msg, "conversation", None)
        self.active[scope_key] = ActiveRun(
            task,
            scope_key,
            session_id,
            started_at=time.time(),
            requester_user_id=getattr(getattr(msg, "sender", None), "open_id", None),
            chat_id=getattr(conversation, "chat_id", None),
            thread_id=getattr(conversation, "thread_id", None),
        )

    async def _scope(self, msg: Any) -> tuple[str, str]:
        conv = getattr(msg, "conversation", None)
        chat = str(getattr(conv, "chat_id", None) or getattr(msg, "chat_id", "unknown"))
        thread = getattr(conv, "thread_id", None) or "root"
        target_type = getattr(self.settings, "target_type", "agent")
        target_id = getattr(self.settings, "target_id", "target")
        user_id = None
        if self._is_group(msg) and getattr(self.settings, "group_session_mode", "user") == "user":
            user_id = getattr(
                getattr(msg, "sender", None) or getattr(msg, "operator", None),
                "open_id",
                None,
            )
        scope = make_scope_key(target_type, target_id, chat, thread, user_id)
        generation = (
            await self.store.get_generation(scope) if hasattr(self.store, "get_generation") else 0
        )
        return scope, make_session_id(scope, generation)

    async def handle_command(self, text: str, msg: Any, scope: str, session_id: str) -> None:
        command, *args = text.split()
        if command == "/help":
            await self._send(msg, help_card())
        elif command == "/new":
            if hasattr(self.store, "increment_generation"):
                await self.store.increment_generation(scope)
            if hasattr(self.store, "clear_hitl_requirements"):
                await self.store.clear_hitl_requirements(scope)
            await self._send(msg, run_card(status="completed", output="已重置会话"))
        elif command in {"/sheet", "/base"}:
            await self._handle_data_command(command, args, msg)
        elif command == "/status":
            run = self.active.get(scope)
            persisted = []
            if hasattr(self.store, "active_runs"):
                persisted = await self.store.active_runs(scope)
            output = (
                f"运行中：{run.run_id or '准备中'}"
                if run
                else (
                    f"运行中：{persisted[0].get('run_id') or '准备中'}"
                    if persisted
                    else "当前没有活动运行"
                )
            )
            active_run_id = (
                run.run_id if run else (persisted[0].get("run_id") if persisted else None)
            )
            await self._send(
                msg,
                run_card(
                    status="running" if (run or persisted) else "completed",
                    output=output,
                    run_id=active_run_id,
                ),
            )
        elif command == "/cancel":
            run = self.active.get(scope)
            if args and (run is None or run.run_id != args[0]):
                run = None
            if run is None and hasattr(self.store, "active_runs"):
                persisted = await self.store.active_runs(scope)
                candidate = next(
                    (item for item in persisted if not args or item.get("run_id") == args[0]),
                    None,
                )
                if candidate is not None and candidate.get("run_id"):
                    run = ActiveRun(
                        task=asyncio.create_task(asyncio.sleep(0)),
                        scope_key=scope,
                        session_id=str(candidate.get("session_id") or session_id),
                        run_id=str(candidate["run_id"]),
                        started_at=float(candidate.get("started_at") or time.time()),
                    )
            if run:
                await self._cancel_remote(run)
                run.task.cancel()
                await self._send(
                    msg, run_card(status="cancelled", run_id=run.run_id, output="取消请求已接受")
                )
            else:
                await self._send(msg, run_card(status="completed", output="当前没有活动运行"))
        elif command in {"/approve", "/reject", "/continue", "/input", "/feedback", "/external"}:
            await self._continue_paused(scope, session_id, msg, command, args)
        else:
            await self._send(msg, help_card())

    async def _handle_data_command(self, command: str, args: list[str], msg: Any) -> None:
        if self.data_client is None:
            await self._send(
                msg, data_card(title="Lark 数据", headers=[], rows=[], error="数据读取未配置")
            )
            return
        try:
            if command == "/sheet":
                if len(args) != 2:
                    raise ValueError("用法：/sheet <URL 或 token> <Sheet!A1:D20>")
                result = await self.data_client.read_sheet_values(args[0], args[1])
                values = result.values
                headers = [str(value) for value in (values[0] if values else [])]
                rows = values[1:] if values else []
                card = data_card(
                    title="Lark/Feishu Sheet", headers=headers, rows=rows,
                    truncated=result.truncated,
                )
            else:
                if len(args) < 2:
                    raise ValueError("用法：/base <URL 或 token> <table_id> [关键词]")
                result = (
                    await self.data_client.search_base_records(args[0], args[1], " ".join(args[2:]))
                    if len(args) > 2
                    else await self.data_client.list_base_records(args[0], args[1])
                )
                records = [item.get("fields", item) for item in result.records]
                headers = list(records[0].keys()) if records else []
                rows = [[item.get(header, "") for header in headers] for item in records]
                card = data_card(
                    title="Lark/Feishu Base", headers=headers, rows=rows,
                    truncated=result.truncated,
                )
            await self._send(msg, card)
        except ValueError as exc:
            await self._send(
                msg, data_card(title="Lark 数据", headers=[], rows=[], error=str(exc))
            )
        except Exception as exc:
            self.last_error = f"data read failed: {type(exc).__name__}"
            log.debug("Lark data read failed", exc_info=True)
            await self._send(
                msg,
                data_card(title="Lark 数据", headers=[], rows=[], error="请检查资源权限和配置"),
            )

    async def _continue_paused(
        self, scope: str, session_id: str, msg: Any, command: str, args: list[str]
    ) -> None:
        rows = (
            await self.store.list_hitl_requirements(scope)
            if hasattr(self.store, "list_hitl_requirements")
            else []
        )
        pending = [row for row in rows if row.get("status") == "pending"]
        if args:
            selected = [
                row
                for row in pending
                if row.get("requirement_id") == args[0] or row.get("run_id") == args[0]
            ]
            pending = selected or pending
        if not pending:
            await self._send(msg, run_card(status="completed", output="没有待处理的审批"))
            return
        row = pending[0]
        try:
            action_args = (
                args[1:]
                if args and args[0] in {row.get("run_id"), row.get("requirement_id")}
                else args
            )
            requester = row.get("requester_user_id")
            actor = getattr(getattr(msg, "sender", None), "open_id", None)
            if requester and actor and requester != actor:
                await self._send(
                    msg,
                    run_card(
                        status="failed", run_id=row.get("run_id"),
                        output="只有原始发起者可以处理此请求",
                    ),
                )
                return
            payload = row["payload"]
            continuation_session = str(row.get("session_id") or session_id)
            if row.get("target_type") and row["target_type"] != getattr(
                self.settings, "target_type", "agent"
            ):
                raise ValueError("target mismatch")
            is_workflow = row.get("requirement_kind") == "workflow" or "step_id" in payload
            requirement: Any
            if is_workflow:
                from agno.workflow.types import StepRequirement

                requirement = StepRequirement.from_dict(payload)
                if command == "/approve":
                    requirement.confirm()
                elif command == "/reject":
                    requirement.reject(" ".join(action_args) if action_args else None)
                elif command in {"/input", "/continue"} and requirement.needs_user_input:
                    values = self._parse_pairs(action_args)
                    if not values and action_args:
                        values = {"input": " ".join(action_args)}
                    requirement.set_user_input(**values)
                elif command in {"/continue", "/feedback"} and requirement.needs_route_selection:
                    choices = action_args
                    if requirement.allow_multiple_selections:
                        requirement.select_multiple(choices)
                    elif choices:
                        requirement.select_single(choices[0])
                elif command == "/external":
                    requirement.executor_requirements = requirement.executor_requirements or []
                requirements: list[Any] = [requirement]
            else:
                from agno.run.requirement import RunRequirement

                requirement = RunRequirement.from_dict(payload)
                if command == "/approve":
                    requirement.confirm()
                elif command == "/reject":
                    requirement.reject(" ".join(action_args) if action_args else None)
                elif command in {"/input", "/continue"} and requirement.needs_user_input:
                    values = self._parse_pairs(action_args)
                    if not values and action_args:
                        values = {"input": " ".join(action_args)}
                    requirement.provide_user_input(values)
                elif command in {"/feedback", "/continue"} and requirement.needs_user_feedback:
                    requirement.provide_user_feedback(self._parse_feedback(action_args))
                elif command in {"/external", "/continue"} and requirement.needs_external_execution:
                    requirement.set_external_execution_result(" ".join(action_args))
                requirements = [requirement]
            if hasattr(self.store, "update_run"):
                if hasattr(self.store, "update_run_by_id"):
                    await self.store.update_run_by_id(scope, row["run_id"], status="continuing")
                else:
                    await self.store.update_run(
                        scope, float(row.get("created_at") or 0), status="continuing"
                    )
            continue_method = getattr(self.target, "acontinue_run", None)
            if callable(continue_method):
                kwargs = {
                    "stream": True,
                    "session_id": continuation_session,
                    "auth_token": self._token_value(),
                }
                target_type = getattr(self.settings, "target_type", "agent")
                if target_type != "workflow":
                    kwargs["user_id"] = actor
                parameters = inspect.signature(continue_method).parameters
                if "run_response" in parameters:
                    kwargs["run_id"] = row["run_id"]
                    if target_type == "workflow":
                        kwargs["step_requirements"] = requirements
                    else:
                        kwargs["requirements"] = requirements
                    result = continue_method(**kwargs)
                elif target_type != "workflow":
                    result = continue_method(row["run_id"], requirements, **kwargs)
                else:
                    result = continue_method(
                        run_id=row["run_id"], step_requirements=requirements, **kwargs
                    )
            else:
                kind = getattr(self.settings, "target_type", "agent") + "s"
                base = str(getattr(self.settings, "agentos_url", "")).rstrip("/")
                target_id = getattr(self.settings, "target_id", "")
                url = f"{base}/{kind}/{target_id}/runs/{row['run_id']}/continue"
                token = self._token_value()
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                async with httpx.AsyncClient() as client:
                    target_type = getattr(self.settings, "target_type", "agent")
                    field = "step_requirements" if target_type == "workflow" else (
                        "tools" if target_type == "agent" else "requirements"
                    )
                    requirement_payload = requirement.to_dict()
                    if target_type == "agent":
                        requirement_payload = requirement_payload.get(
                            "tool_execution", requirement_payload
                        )
                    response = await client.post(
                        url,
                        params={"session_id": continuation_session},
                        data={field: json.dumps([requirement_payload], ensure_ascii=False)},
                        headers=headers,
                    )
                    response.raise_for_status()
                    result = response.json()
            if inspect.isawaitable(result):
                result = await result
            continuation_status = "completed"
            continuation_output = ""
            if hasattr(result, "__aiter__"):
                async for event in result:
                    event_name = str(getattr(event, "event", "")).lower()
                    if event_name in {"runerror", "error", "steperror"}:
                        continuation_status = "failed"
                    elif event_name in {"runcancelled", "cancelled"}:
                        continuation_status = "cancelled"
                    if event_name in {"runpaused", "paused", "workflowpaused", "steppaused"}:
                        continuation_status = "paused"
                        next_requirements = getattr(event, "requirements", None) or getattr(
                            event, "step_requirements", None
                        ) or []
                        for next_requirement in next_requirements:
                            if hasattr(self.store, "save_hitl_requirement"):
                                await self.store.save_hitl_requirement(
                                    next_requirement,
                                    run_id=row["run_id"],
                                    scope_key=scope,
                                    requirement_kind=(
                                        "workflow"
                                        if "step" in event_name or "workflow" in event_name
                                        else "agent"
                                    ),
                                    requester_user_id=requester,
                                    chat_id=row.get("chat_id"),
                                    thread_id=row.get("thread_id"),
                                    session_id=continuation_session,
                                    target_type=getattr(self.settings, "target_type", "agent"),
                                    target_id=getattr(self.settings, "target_id", ""),
                                )
                        self.pending_hitl += len(next_requirements)
                    content = getattr(event, "content", None)
                    if content:
                        content = str(content)
                        if continuation_output and content.startswith(continuation_output):
                            continuation_output = content
                        else:
                            continuation_output += content
                        await self._send(
                            msg,
                            run_card(
                                status="running",
                                run_id=row["run_id"],
                                output=continuation_output,
                            ),
                        )
            if hasattr(self.store, "update_run_by_id"):
                await self.store.update_run_by_id(
                    scope, row["run_id"], status=continuation_status,
                    finished_at=None if continuation_status == "paused" else time.time(),
                )
            resolved_attr = getattr(requirement, "is_resolved", None)
            resolved = (
                bool(resolved_attr())
                if callable(resolved_attr)
                else bool(resolved_attr if resolved_attr is not None else True)
            )
            if hasattr(self.store, "update_hitl_requirement") and resolved:
                self.pending_hitl = max(0, self.pending_hitl - 1)
                await self.store.update_hitl_requirement(
                    scope,
                    row["run_id"],
                    row["requirement_id"],
                    status="resolved",
                    payload=requirement.to_dict(),
                )
            await self._send(
                msg,
                run_card(
                    status="running" if continuation_status == "completed" else continuation_status,
                    run_id=row["run_id"],
                    output="已提交，正在继续"
                    if continuation_status == "completed"
                    else (
                        "运行已暂停，请继续处理"
                        if continuation_status == "paused"
                        else "运行已取消"
                        if continuation_status == "cancelled"
                        else "继续运行失败"
                    ),
                ),
            )
        except Exception as exc:
            self.last_error = f"HITL continuation failed: {type(exc).__name__}"
            log.debug("HITL continuation failed", exc_info=True)
            await self._send(
                msg, run_card(status="failed", run_id=row.get("run_id"), output="继续运行失败")
            )

    @staticmethod
    def _parse_pairs(values: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for value in values:
            if "=" in value:
                key, item = value.split("=", 1)
                result[key] = item
        return result

    @staticmethod
    def _parse_feedback(values: list[str]) -> dict[str, list[str]]:
        return {key: [item] for key, item in LarkBridge._parse_pairs(values).items()}

    def _token_value(self) -> str | None:
        token = getattr(self.settings, "agentos_token", None)
        getter = getattr(token, "get_secret_value", None)
        return getter() if callable(getter) else (str(token) if token else None)

    async def _run(self, msg: Any, text: str, scope: str, session_id: str) -> None:
        active = self.active[scope]
        lock = self._locks.setdefault(scope, asyncio.Lock())
        conv = getattr(msg, "conversation", None)
        record = RunRecord(
            scope_key=scope,
            run_id=None,
            session_id=session_id,
            target_type=getattr(self.settings, "target_type", "agent"),
            target_id=getattr(self.settings, "target_id", ""),
            chat_id=getattr(conv, "chat_id", ""),
            thread_id=getattr(conv, "thread_id", None),
            started_at=active.started_at,
        )
        try:
            async with lock:
                if hasattr(self.store, "record_run"):
                    await self.store.record_run(record)
            kwargs = {
                "stream": True,
                "user_id": getattr(getattr(msg, "sender", None), "open_id", None),
                "session_id": session_id,
                # RemoteAgent/RemoteTeam/RemoteWorkflow keep session history
                # by default, but do not add it to the next model request
                # unless this flag is enabled.  The bridge maps each Lark
                # conversation to a stable session, so opt in explicitly to
                # make that history available to the target on every turn.
                "add_history_to_context": True,
            }
            token = getattr(self.settings, "agentos_token", None)
            if token:
                kwargs["auth_token"] = (
                    token.get_secret_value() if hasattr(token, "get_secret_value") else token
                )
            resources = getattr(msg, "resources", None) or []
            if resources:
                images: list[Any] = []
                files: list[Any] = []
                for resource in resources:
                    media = await resource_to_media(self.channel, getattr(msg, "id", ""), resource)
                    (images if media.__class__.__name__ == "Image" else files).append(media)
                kwargs["images"], kwargs["files"] = images, files
            output = ""
            run_status = "completed"
            run_error: str | None = None
            paused_requirement_ids: list[str] = []
            destination = getattr(getattr(msg, "conversation", None), "chat_id", None) or getattr(
                msg, "chat_id", None
            )

            async def consume(update: Any | None = None) -> None:
                nonlocal output, run_status, run_error
                result = self.target.arun(text, **kwargs)
                if inspect.isawaitable(result):
                    result = await result

                async def process_event(event: Any) -> None:
                    nonlocal output, run_status, run_error
                    rid = getattr(event, "run_id", None)
                    if rid:
                        active.run_id = rid
                        if hasattr(self.store, "update_run"):
                            await self.store.update_run(scope, active.started_at, run_id=rid)
                    event_name = str(getattr(event, "event", "")).lower()
                    if event_name in {"runerror", "error"}:
                        run_status = "failed"
                        run_error = str(
                            getattr(event, "error_type", None)
                            or getattr(event, "content", None)
                            or "AgentOS run error"
                        )
                        return
                    if event_name in {"runcancelled", "cancelled"}:
                        run_status = "cancelled"
                        run_error = str(getattr(event, "reason", None) or "run cancelled")
                        return
                    if event_name in {
                        "runpaused",
                        "paused",
                        "workflowpaused",
                        "steppaused",
                        "routerpaused",
                        "stepexecutorpaused",
                    }:
                        run_status = "paused"
                        run_error = str(getattr(event, "content", None) or "run paused")
                        requirements = getattr(event, "requirements", None) or getattr(
                            event, "active_requirements", None
                        ) or getattr(event, "step_requirements", None) or []
                        requirement_kind = (
                            "workflow"
                            if getattr(event, "step_requirements", None) is not None
                            else "agent"
                        )
                        for requirement in requirements:
                            payload = (
                                requirement.to_dict() if hasattr(requirement, "to_dict") else {}
                            )
                            paused_requirement_ids.append(
                                str(
                                    getattr(requirement, "id", None)
                                    or payload.get("id")
                                    or payload.get("step_id")
                                    or ""
                                )
                            )
                            if hasattr(self.store, "save_hitl_requirement"):
                                await self.store.save_hitl_requirement(
                                    requirement,
                                    run_id=active.run_id or "",
                                    scope_key=scope,
                                    requirement_kind=requirement_kind,
                                    requester_user_id=active.requester_user_id,
                                    chat_id=active.chat_id,
                                    thread_id=active.thread_id,
                                    session_id=active.session_id,
                                    target_type=getattr(self.settings, "target_type", "agent"),
                                    target_id=getattr(self.settings, "target_id", ""),
                                )
                        self.pending_hitl += len(requirements)
                        return
                    if event_name in {"runcompleted", "completed"}:
                        run_status = "completed"
                    delta = getattr(event, "content", None) or getattr(event, "delta", None) or ""
                    if not isinstance(delta, str):
                        delta = str(delta) if delta else ""
                    if output and delta.startswith(output):
                        output = delta
                    elif delta:
                        output += delta
                    if update is not None and delta:
                        await update(
                            run_card(
                                status="running",
                                output=output,
                                run_id=active.run_id,
                                elapsed=time.time() - active.started_at,
                            )
                        )

                if hasattr(result, "__aiter__"):
                    async for event in result:
                        await process_event(event)
                else:
                    await process_event(result)

            def final_output() -> str:
                if run_status == "failed":
                    return "运行失败"
                if run_status == "cancelled":
                    return "运行已取消"
                if run_status == "paused":
                    return "运行已暂停，请稍后继续处理"
                return output

            stream = getattr(self.channel, "stream", None)
            if callable(stream):

                async def producer(controller: Any) -> None:
                    await consume(controller.update)
                    await controller.update(
                        run_card(
                            status=run_status,
                            output=final_output(),
                            run_id=active.run_id,
                            elapsed=time.time() - active.started_at,
                            requirement_ids=paused_requirement_ids,
                        )
                    )

                stream_result = stream(
                    destination,
                    {
                        "card": {
                            "initial": run_card(status="running"),
                            "producer": producer,
                        }
                    },
                )
                if inspect.isawaitable(stream_result):
                    await stream_result
            else:
                await consume()
                await self._send(
                    msg,
                    run_card(
                        status=run_status,
                        output=final_output(),
                        run_id=active.run_id,
                        elapsed=time.time() - active.started_at,
                        requirement_ids=paused_requirement_ids,
                    ),
                )
            if hasattr(self.store, "update_run"):
                changes: dict[str, Any] = {
                    "status": run_status,
                    "finished_at": time.time(),
                }
                if run_error:
                    changes["error"] = run_error
                await self.store.update_run(scope, active.started_at, **changes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            await self._send(
                msg, run_card(status="failed", output="运行失败", run_id=active.run_id)
            )
            if hasattr(self.store, "update_run"):
                await self.store.update_run(
                    scope,
                    active.started_at,
                    status="failed",
                    finished_at=time.time(),
                    error=str(exc),
                )
        finally:
            self.active.pop(scope, None)

    async def _cancel_remote(self, run: ActiveRun) -> None:
        if hasattr(self.store, "update_run"):
            await self.store.update_run(run.scope_key, run.started_at, status="cancel_requested")
        if run.run_id:
            try:
                kind = getattr(self.settings, "target_type", "agent") + "s"
                base = str(getattr(self.settings, "agentos_url", "")).rstrip("/")
                target_id = getattr(self.settings, "target_id", "")
                url = f"{base}/{kind}/{target_id}/runs/{run.run_id}/cancel"
                token = getattr(self.settings, "agentos_token", None)
                getter = getattr(token, "get_secret_value", None)
                token_value = getter() if callable(getter) else str(token)
                headers = {"Authorization": f"Bearer {token_value}"} if token else {}

                async def post_cancel(*, trust_env: bool) -> None:
                    async with httpx.AsyncClient(trust_env=trust_env) as client:
                        response = await client.post(
                            url, params={"session_id": run.session_id}, headers=headers
                        )
                        response.raise_for_status()

                try:
                    await post_cancel(trust_env=True)
                except ImportError as exc:
                    # HTTPX requires the optional ``socksio`` package when a
                    # SOCKS proxy is present in the environment. Retry directly
                    # so local AgentOS deployments and mocked tests still work.
                    if "socksio" not in str(exc).lower():
                        raise
                    await post_cancel(trust_env=False)
            except Exception as exc:
                # Keep cancellation failures visible through ``/status`` while
                # avoiding the URL or authorization header in user-facing state.
                detail = str(exc).replace("\n", " ").strip()
                if len(detail) > 160:
                    detail = detail[:157] + "..."
                self.last_error = f"remote cancellation failed: {type(exc).__name__}" + (
                    f": {detail}" if detail else ""
                )
                log.debug("remote cancellation failed", exc_info=True)

    async def handle_card_action(self, event: Any) -> None:
        data = getattr(event, "action", None) or getattr(event, "value", None) or {}
        data = getattr(data, "value", data)
        if isinstance(data, str):
            return
        action = data.get("action")
        if isinstance(action, str) and action.startswith("hitl:"):
            action = action.split(":", 1)[1]
        msg = getattr(event, "message", None) or event
        if not self._allowed(msg):
            return
        scope, session = await self._scope(msg)
        if action == "status":
            await self.handle_command("/status", msg, scope, session)
        elif action == "cancel":
            run_id = data.get("run_id")
            await self.handle_command(
                f"/cancel {run_id}" if run_id else "/cancel", msg, scope, session
            )
        elif action in {"approve", "reject", "continue"}:
            run_id = data.get("run_id")
            requirement_id = data.get("requirement_id")
            command_args = " ".join(
                str(item) for item in (requirement_id, run_id) if item
            )
            await self.handle_command(
                f"/{action} {command_args}" if command_args else f"/{action}",
                msg,
                scope,
                session,
            )

    async def _send(self, msg: Any, card: dict[str, Any]) -> None:
        to = getattr(getattr(msg, "conversation", None), "chat_id", None) or getattr(
            msg, "chat_id", None
        )
        try:
            # FeishuChannel.send accepts an outbound envelope.  ``card`` is
            # the schema-2.0 card document itself and must be nested under the
            # SDK's ``card`` key; passing the document directly makes the
            # coercer reject its top-level ``schema`` and ``body`` keys.
            result = self.channel.send(to, {"card": card})
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self.last_error = str(exc)

    def status(self) -> dict[str, Any]:
        connected = getattr(self.channel, "connected", None)
        if connected is None:
            ready = getattr(self.channel, "is_ready", None)
            try:
                connected = bool(ready() if callable(ready) else ready)
            except Exception:
                connected = False
        result = {
            "connected": bool(connected),
            "target_type": getattr(self.settings, "target_type", "agent"),
            "target_id": getattr(self.settings, "target_id", ""),
            "active_runs": len(self.active),
            "last_error": self.last_error,
            "received_messages": self.received_messages,
            "rejected_messages": self.rejected_messages,
            "filtered_messages": self.filtered_messages,
            "last_reject_reason": self.last_reject_reason,
            "pending_hitl": self.pending_hitl,
        }
        snapshot = getattr(self.channel, "connection_snapshot", None)
        if callable(snapshot):
            try:
                state = snapshot()
                result["connection_state"] = getattr(state, "state", None)
                result["reconnect_attempts"] = getattr(state, "reconnect_attempts", 0)
            except Exception:
                # Status must remain available even when an SDK version does
                # not expose a stable connection snapshot implementation.
                pass
        return result

    async def status_snapshot(self) -> dict[str, Any]:
        result = self.status()
        counter = getattr(self.store, "pending_hitl_count", None)
        if callable(counter):
            result["pending_hitl"] = await counter()
        return result


# Backward-compatible name for integrations that used the original Feishu branding.
FeishuBridge = LarkBridge

__all__ = ["ActiveRun", "LarkBridge", "FeishuBridge"]
