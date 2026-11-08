"""Lark/Feishu event bridge and AgentOS execution orchestration."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from lark_channel import Events  # type: ignore[import-untyped]

from .cards import help_card, run_card
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


class LarkBridge:
    def __init__(self, *, target: Any, settings: Any, store: Any, channel: Any) -> None:
        self.target, self.settings, self.store, self.channel = target, settings, store, channel
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
        self.active[scope_key] = ActiveRun(task, scope_key, session_id, started_at=time.time())

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
            await self._send(msg, run_card(status="completed", output="已重置会话"))
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
        else:
            await self._send(msg, help_card())

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
                    if event_name in {"runpaused", "paused"}:
                        run_status = "paused"
                        run_error = str(getattr(event, "content", None) or "run paused")
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
                self.last_error = (
                    f"remote cancellation failed: {type(exc).__name__}"
                    + (f": {detail}" if detail else "")
                )
                log.debug("remote cancellation failed", exc_info=True)

    async def handle_card_action(self, event: Any) -> None:
        data = getattr(event, "action", None) or getattr(event, "value", None) or {}
        data = getattr(data, "value", data)
        if isinstance(data, str):
            return
        action = data.get("action")
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

# Backward-compatible name for integrations that used the original Feishu branding.
FeishuBridge = LarkBridge

__all__ = ["ActiveRun", "LarkBridge", "FeishuBridge"]
