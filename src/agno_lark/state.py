"""Persistent SQLite state used by the bridge."""

from __future__ import annotations

import asyncio
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
        """)
        await self._db.execute(
            "INSERT INTO schema_version(version) SELECT 1 "
            "WHERE NOT EXISTS (SELECT 1 FROM schema_version)"
        )
        await self._db.execute("DELETE FROM dedup_messages WHERE expires_at < ?", (time.time(),))
        await self._db.execute(
            "DELETE FROM runs WHERE started_at < ?", (time.time() - self.run_retention,)
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
            "AND status IN ('running','cancel_requested','paused') "
            "ORDER BY started_at DESC",
            (scope_key,),
        )
        return [dict(row) for row in await cur.fetchall()]
