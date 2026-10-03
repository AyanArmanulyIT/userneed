"""
SQLite FSM storage для aiogram 3.

Заменяет MemoryStorage, чтобы состояния не терялись при перезапуске бота.
Не требует Redis — использует aiosqlite и один файл на диске.

Формат:
  fsm(user_id INTEGER, chat_id INTEGER, bot_id INTEGER,
      state TEXT, data TEXT,
      PRIMARY KEY (user_id, chat_id, bot_id))
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

logger = logging.getLogger(__name__)


class SQLiteStorage(BaseStorage):
    def __init__(self, path: str | Path = "fsm.db") -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS fsm (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    bot_id INTEGER NOT NULL,
                    state TEXT,
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (user_id, chat_id, bot_id)
                )
            """)
            await self._db.commit()
        return self._db

    @staticmethod
    def _key_parts(key: StorageKey) -> tuple[int, int, int]:
        return (key.user_id, key.chat_id, key.bot_id)

    async def set_state(self, key: StorageKey, state: State | str | None = None) -> None:
        state_value = state.state if isinstance(state, State) else state
        async with self._lock:
            db = await self._get_db()
            uid, cid, bid = self._key_parts(key)
            await db.execute(
                """
                INSERT INTO fsm (user_id, chat_id, bot_id, state, data)
                VALUES (?, ?, ?, ?, '{}')
                ON CONFLICT(user_id, chat_id, bot_id)
                DO UPDATE SET state = excluded.state
                """,
                (uid, cid, bid, state_value),
            )
            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._lock:
            db = await self._get_db()
            uid, cid, bid = self._key_parts(key)
            cur = await db.execute(
                "SELECT state FROM fsm WHERE user_id=? AND chat_id=? AND bot_id=?",
                (uid, cid, bid),
            )
            row = await cur.fetchone()
            return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        async with self._lock:
            db = await self._get_db()
            uid, cid, bid = self._key_parts(key)
            payload = json.dumps(data, ensure_ascii=False)
            await db.execute(
                """
                INSERT INTO fsm (user_id, chat_id, bot_id, state, data)
                VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(user_id, chat_id, bot_id)
                DO UPDATE SET data = excluded.data
                """,
                (uid, cid, bid, payload),
            )
            await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._lock:
            db = await self._get_db()
            uid, cid, bid = self._key_parts(key)
            cur = await db.execute(
                "SELECT data FROM fsm WHERE user_id=? AND chat_id=? AND bot_id=?",
                (uid, cid, bid),
            )
            row = await cur.fetchone()
            if not row or not row["data"]:
                return {}
            try:
                return json.loads(row["data"])
            except Exception:
                return {}

    async def close(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            finally:
                self._db = None 