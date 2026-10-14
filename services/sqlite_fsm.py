"""
SQLite FSM storage для aiogram 3 (fsm.db).

Заменяет MemoryStorage, чтобы состояния не терялись при перезапуске бота.
Не требует Redis — использует aiosqlite и один файл на диске.

Ключ записи — ВСЕ поля StorageKey (bot_id, chat_id, user_id, thread_id,
business_connection_id, destiny), склеенные в строку. Старая версия
учитывала только три поля и смешивала контексты разных тем/destiny.

Гигиена:
  • если и state, и data пусты — строка удаляется, а не остаётся навсегда;
  • cleanup(max_age_days) удаляет записи, которые давно не трогали
    (брошенные диалоги) — вызывается из main.py при старте.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

logger = logging.getLogger(__name__)

_TABLE = "fsm_state"


class SQLiteStorage(BaseStorage):
    def __init__(self, path: str | Path = "fsm.db") -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # --- Подключение / схема --------------------------------------------------

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            db = await aiosqlite.connect(self._path)
            db.row_factory = aiosqlite.Row
            await db.execute(f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    key        TEXT PRIMARY KEY,
                    state      TEXT,
                    data       TEXT NOT NULL DEFAULT '{{}}',
                    updated_at REAL NOT NULL DEFAULT 0
                )
            """)
            await db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_updated ON {_TABLE} (updated_at)"
            )
            # Старая таблица с неполным ключом больше не используется.
            await db.execute("DROP TABLE IF EXISTS fsm")
            await db.commit()
            self._db = db
            logger.info("FSM storage: %s", self._path)
        return self._db

    @staticmethod
    def _key(key: StorageKey) -> str:
        return ":".join(
            str(part) if part is not None else ""
            for part in (
                key.bot_id,
                key.chat_id,
                key.user_id,
                key.thread_id,
                key.business_connection_id,
                key.destiny,
            )
        )

    async def _fetch(self, db: aiosqlite.Connection, k: str) -> aiosqlite.Row | None:
        cur = await db.execute(
            f"SELECT state, data FROM {_TABLE} WHERE key = ?", (k,)
        )
        return await cur.fetchone()

    async def _write(
        self,
        db: aiosqlite.Connection,
        k: str,
        state: str | None,
        data_json: str,
    ) -> None:
        """Пишет строку; если она стала пустой — удаляет."""
        if state is None and data_json in ("{}", ""):
            await db.execute(f"DELETE FROM {_TABLE} WHERE key = ?", (k,))
        else:
            await db.execute(
                f"""
                INSERT INTO {_TABLE} (key, state, data, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    state = excluded.state,
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (k, state, data_json, time.time()),
            )
        await db.commit()

    # --- BaseStorage ---------------------------------------------------------

    async def set_state(self, key: StorageKey, state: State | str | None = None) -> None:
        state_value = state.state if isinstance(state, State) else state
        k = self._key(key)
        async with self._lock:
            db = await self._get_db()
            row = await self._fetch(db, k)
            data_json = row["data"] if row and row["data"] else "{}"
            await self._write(db, k, state_value, data_json)

    async def get_state(self, key: StorageKey) -> str | None:
        k = self._key(key)
        async with self._lock:
            db = await self._get_db()
            row = await self._fetch(db, k)
            return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        k = self._key(key)
        payload = json.dumps(data or {}, ensure_ascii=False)
        async with self._lock:
            db = await self._get_db()
            row = await self._fetch(db, k)
            state_value = row["state"] if row else None
            await self._write(db, k, state_value, payload)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        k = self._key(key)
        async with self._lock:
            db = await self._get_db()
            row = await self._fetch(db, k)
        if not row or not row["data"]:
            return {}
        try:
            loaded = json.loads(row["data"])
        except (TypeError, ValueError):
            logger.warning("FSM: повреждённые данные для ключа %s — сброшены", k)
            return {}
        return loaded if isinstance(loaded, dict) else {}

    # --- Обслуживание --------------------------------------------------------

    async def cleanup(self, max_age_days: int = 7) -> int:
        """Удаляет записи, которые не обновлялись дольше max_age_days."""
        cutoff = time.time() - max(1, max_age_days) * 86400
        async with self._lock:
            db = await self._get_db()
            cur = await db.execute(
                f"DELETE FROM {_TABLE} WHERE updated_at < ?", (cutoff,)
            )
            await db.commit()
            removed = cur.rowcount or 0
        if removed:
            logger.info("FSM: удалено %d брошенных состояний", removed)
        return removed

    async def close(self) -> None:
        async with self._lock:
            if self._db is not None:
                try:
                    await self._db.close()
                finally:
                    self._db = None
