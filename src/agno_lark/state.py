"""Persistent SQLite state used by the bridge."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite


@dataclass
class RunRecord:
    scope_key: str
    run_id: str | None
    session_id: str
    target_type: str
    target_id: str
    chat_id: str
    thread_id: str | None
    status: str = "running"
    started_at: float = 0.0
    finished_at: float | None = None
    error: str | None = None


@dataclass
class HitlRequirementRecord:
    scope_key: str
    run_id: str
    requirement_id: str
    payload: dict[str, Any]
    status: str = "pending"
    created_at: float = 0.0
    resolved_at: float | None = None
    requirement_kind: str = "agent"
    requester_user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    resolved_by_user_id: str | None = None
    session_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None


class SqliteStateStore:
    def __init__(
        self, path: str | Path, dedup_ttl: int = 86400, run_retention: int = 604800
    ) -> None:
        self.path = Path(path)
        self.dedup_ttl = dedup_ttl
        self.run_retention = run_retention
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS dedup_messages (
                message_id TEXT PRIMARY KEY, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_scopes (
                scope_key TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                scope_key TEXT NOT NULL, run_id TEXT, session_id TEXT NOT NULL,
                target_type TEXT NOT NULL, target_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                thread_id TEXT, status TEXT NOT NULL, started_at REAL NOT NULL,
                finished_at REAL, error TEXT, PRIMARY KEY(scope_key, started_at)
            );
            CREATE TABLE IF NOT EXISTS hitl_requirements (
                scope_key TEXT NOT NULL, run_id TEXT NOT NULL,
                requirement_id TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL,
                resolved_at REAL, requirement_kind TEXT NOT NULL DEFAULT 'agent',
                requester_user_id TEXT, chat_id TEXT, thread_id TEXT,
                resolved_by_user_id TEXT,
                session_id TEXT, target_type TEXT, target_id TEXT,
                PRIMARY KEY(scope_key, run_id, requirement_id)
            );
        """)
        await self._db.execute(
            "INSERT INTO schema_version(version) SELECT 1 "
            "WHERE NOT EXISTS (SELECT 1 FROM schema_version)"
        )
        await self._db.execute("UPDATE schema_version SET version = 2 WHERE version < 2")
        # Existing v1 databases may already have the table with the original
        # seven columns.  Add the v2 metadata columns idempotently.
        cur = await self._db.execute("PRAGMA table_info(hitl_requirements)")
        columns = {str(row[1]) for row in await cur.fetchall()}
        for name, definition in (
            ("requirement_kind", "TEXT NOT NULL DEFAULT 'agent'"),
            ("requester_user_id", "TEXT"),
            ("chat_id", "TEXT"),
            ("thread_id", "TEXT"),
            ("resolved_by_user_id", "TEXT"),
            ("session_id", "TEXT"),
            ("target_type", "TEXT"),
            ("target_id", "TEXT"),
        ):
            if name not in columns:
                await self._db.execute(
                    f"ALTER TABLE hitl_requirements ADD COLUMN {name} {definition}"
                )
        await self._db.execute("DELETE FROM dedup_messages WHERE expires_at < ?", (time.time(),))
        await self._db.execute(
            "DELETE FROM runs WHERE started_at < ?", (time.time() - self.run_retention,)
        )
        await self._db.execute(
            "DELETE FROM hitl_requirements WHERE created_at < ?",
            (time.time() - self.run_retention,),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _conn(self) -> aiosqlite.Connection:
        await self.open()
        assert self._db is not None
        return self._db

    async def seen_message(self, message_id: str) -> bool:
        async with self._write_lock:
            db = await self._conn()
            now = time.time()
            await db.execute("DELETE FROM dedup_messages WHERE expires_at < ?", (now,))
            cur = await db.execute(
                "SELECT 1 FROM dedup_messages WHERE message_id = ?", (message_id,)
            )
            found = await cur.fetchone()
            if found:
                return True
            await db.execute(
                "INSERT OR REPLACE INTO dedup_messages VALUES (?, ?)",
                (message_id, now + self.dedup_ttl),
            )
            await db.commit()
            return False

    async def check_and_record_message(self, message_id: str) -> bool:
        """Return whether a message was already seen, recording new IDs."""
        return await self.seen_message(message_id)

    async def get_generation(self, scope_key: str) -> int:
        db = await self._conn()
        cur = await db.execute(
            "SELECT generation FROM session_scopes WHERE scope_key = ?", (scope_key,)
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def increment_generation(self, scope_key: str) -> int:
        async with self._write_lock:
            db = await self._conn()
            now = time.time()
            cur = await db.execute(
                "SELECT generation FROM session_scopes WHERE scope_key = ?", (scope_key,)
            )
            row = await cur.fetchone()
            generation = int(row[0]) + 1 if row else 1
            await db.execute(
                "INSERT OR REPLACE INTO session_scopes VALUES (?, ?, ?)",
                (scope_key, generation, now),
            )
            await db.commit()
            return generation

    async def record_run(self, record: RunRecord) -> None:
        async with self._write_lock:
            db = await self._conn()
            record.started_at = record.started_at or time.time()
            await db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(record.__dict__.values()),
            )
            await db.commit()

    async def update_run(self, scope_key: str, started_at: float, **changes: Any) -> None:
        if not changes:
            return
        async with self._write_lock:
            db = await self._conn()
            assignments = ", ".join(f"{key} = ?" for key in changes)
            await db.execute(
                f"UPDATE runs SET {assignments} WHERE scope_key = ? AND started_at = ?",
                (*changes.values(), scope_key, started_at),
            )
            await db.commit()

    async def update_run_by_id(self, scope_key: str, run_id: str, **changes: Any) -> None:
        if not changes:
            return
        async with self._write_lock:
            db = await self._conn()
            assignments = ", ".join(f"{key} = ?" for key in changes)
            await db.execute(
                f"UPDATE runs SET {assignments} WHERE scope_key = ? AND run_id = ?",
                (*changes.values(), scope_key, run_id),
            )
            await db.commit()

    async def list_runs(self, scope_key: str, limit: int = 20) -> list[dict[str, Any]]:
        db = await self._conn()
        cur = await db.execute(
            "SELECT * FROM runs WHERE scope_key = ? ORDER BY started_at DESC LIMIT ?",
            (scope_key, limit),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def active_runs(self, scope_key: str) -> list[dict[str, Any]]:
        db = await self._conn()
        cur = await db.execute(
            "SELECT * FROM runs WHERE scope_key = ? "
            "AND status IN ('running','continuing','cancel_requested','paused') "
            "ORDER BY started_at DESC",
            (scope_key,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def save_hitl_requirement(
        self,
        record: HitlRequirementRecord | Any,
        *,
        run_id: str | None = None,
        scope_key: str | None = None,
        requirement_kind: str = "agent",
        requester_user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        session_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> None:
        """Persist a RunRequirement or a record without coupling storage to Agno internals."""
        if not isinstance(record, HitlRequirementRecord):
            payload = record.to_dict() if hasattr(record, "to_dict") else dict(record)
            record = HitlRequirementRecord(
                scope_key=scope_key or "",
                run_id=run_id or "",
                requirement_id=str(
                    getattr(record, "id", None)
                    or payload.get("id")
                    or payload.get("step_id")
                    or "unknown"
                ),
                payload=payload,
                requirement_kind=requirement_kind,
                requester_user_id=requester_user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                session_id=session_id,
                target_type=target_type,
                target_id=target_id,
            )
        async with self._write_lock:
            db = await self._conn()
            created = record.created_at or time.time()
            await db.execute(
                "INSERT OR REPLACE INTO hitl_requirements "
                "(scope_key, run_id, requirement_id, payload, status, created_at, resolved_at, "
                "requirement_kind, requester_user_id, chat_id, thread_id, resolved_by_user_id, "
                "session_id, target_type, target_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.scope_key,
                    record.run_id,
                    record.requirement_id,
                    json.dumps(record.payload, ensure_ascii=False),
                    record.status,
                    created,
                    record.resolved_at,
                    record.requirement_kind,
                    record.requester_user_id,
                    record.chat_id,
                    record.thread_id,
                    record.resolved_by_user_id,
                    record.session_id,
                    record.target_type,
                    record.target_id,
                ),
            )
            await db.commit()

    async def list_hitl_requirements(
        self, scope_key: str, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        db = await self._conn()
        if run_id is None:
            cur = await db.execute(
                "SELECT * FROM hitl_requirements WHERE scope_key = ? ORDER BY created_at",
                (scope_key,),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM hitl_requirements WHERE scope_key = ? AND run_id = ? "
                "ORDER BY created_at",
                (scope_key, run_id),
            )
        rows = []
        for row in await cur.fetchall():
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            rows.append(item)
        return rows

    async def get_hitl_requirement(
        self, scope_key: str, run_id: str, requirement_id: str
    ) -> dict[str, Any] | None:
        rows = await self.list_hitl_requirements(scope_key, run_id)
        return next((row for row in rows if row["requirement_id"] == requirement_id), None)

    async def update_hitl_requirement(
        self,
        scope_key: str,
        run_id: str,
        requirement_id: str,
        *,
        status: str = "resolved",
        payload: dict[str, Any] | None = None,
        resolved_by_user_id: str | None = None,
    ) -> None:
        async with self._write_lock:
            db = await self._conn()
            fields = ["status = ?", "resolved_at = ?", "resolved_by_user_id = ?"]
            values: list[Any] = [status, time.time(), resolved_by_user_id]
            if payload is not None:
                fields.append("payload = ?")
                values.append(json.dumps(payload, ensure_ascii=False))
            values.extend([scope_key, run_id, requirement_id])
            await db.execute(
                f"UPDATE hitl_requirements SET {', '.join(fields)} "
                "WHERE scope_key = ? AND run_id = ? AND requirement_id = ?",
                values,
            )
            await db.commit()

    async def clear_hitl_requirements(self, scope_key: str) -> None:
        """Invalidate pending HITL actions when a scope starts a new generation."""
        async with self._write_lock:
            db = await self._conn()
            await db.execute(
                "UPDATE hitl_requirements SET status = 'superseded', resolved_at = ? "
                "WHERE scope_key = ? AND status = 'pending'",
                (time.time(), scope_key),
            )
            await db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, error = ? "
                "WHERE scope_key = ? AND status = 'paused'",
                (time.time(), "superseded by /new", scope_key),
            )
            await db.commit()

    async def pending_hitl_count(self) -> int:
        db = await self._conn()
        cur = await db.execute(
            "SELECT COUNT(*) FROM hitl_requirements WHERE status = 'pending'"
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
