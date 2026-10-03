"""
CheckCache — SQLite-кэш результатов проверок username.

Зачем: 80% всех проверок — занятые имена. Если @vital занят сегодня,
он будет занят и через час. Нет смысла ходить в Telegram каждый раз.

Что кэшируем:
  • taken    — 24 часа  (занятое за день не освободится)
  • invalid  — 7 дней   (формат не меняется)
  • fragment — 12 часов (только за TON, может выкупить кто-то)

Что НЕ кэшируем:
  • free  — может стать занятым в любой момент
  • error — мог быть временной сетевой ошибкой
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# TTL по статусам (в секундах)
_TTL = {
    "taken": 86400,      # 24 часа
    "invalid": 604800,   # 7 дней
    "fragment": 43200,   # 12 часов
}

# Периодическая очистка старых записей.
_CLEAN_INTERVAL = 3600   # раз в час
_RETENTION = 86400 * 7   # хранить 7 дней


class CheckCache:
    """Синхронный SQLite-кэш. Работает с asyncio без executor — операции ~микросекунды."""

    def __init__(self, path: Path | str = "check_cache.db") -> None:
        self._path = Path(path)
        self._last_clean = 0.0
        self._hits = 0
        self._misses = 0
        try:
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
            logger.info("Кэш проверок: %s", self._path)
        except Exception as exc:
            logger.exception("Не удалось открыть кэш %s: %s", self._path, exc)
            self._db = None  # type: ignore[assignment]

    def _init_schema(self) -> None:
        assert self._db is not None
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS checked (
                    username  TEXT PRIMARY KEY,
                    status    TEXT NOT NULL,
                    cached_at REAL NOT NULL
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_checked_time
                    ON checked (cached_at)
            """)

    # --- Публичный API -------------------------------------------------------

    def get(self, username: str) -> str | None:
        """
        Возвращает кэшированный статус, если он свежий, иначе None.
        Обновляет счётчики hits/misses.
        """
        if self._db is None:
            self._misses += 1
            return None

        username = username.lstrip("@").strip().lower()
        try:
            cur = self._db.execute(
                "SELECT status, cached_at FROM checked WHERE username = ?",
                (username,),
            )
            row = cur.fetchone()
            if row is None:
                self._misses += 1
                return None

            status = row["status"]
            ttl = _TTL.get(status)
            if ttl is None:
                # Не должны попасть сюда, но защита.
                self._misses += 1
                return None

            if time.time() - row["cached_at"] > ttl:
                # Устарело — удалить и промах.
                with self._db:
                    self._db.execute(
                        "DELETE FROM checked WHERE username = ?", (username,)
                    )
                self._misses += 1
                return None

            self._hits += 1
            return status
        except Exception:
            logger.exception("cache.get(%s) упал", username)
            self._misses += 1
            return None

    def set(self, username: str, status: str) -> None:
        """Сохраняет результат, если он устойчивый (taken / invalid / fragment)."""
        if self._db is None:
            return
        if status not in _TTL:
            return
        username = username.lstrip("@").strip().lower()
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO checked (username, status, cached_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(username)
                    DO UPDATE SET status = excluded.status,
                                  cached_at = excluded.cached_at
                    """,
                    (username, status, time.time()),
                )
        except Exception:
            logger.exception("cache.set(%s) упал", username)

        # Периодическая очистка.
        now = time.time()
        if now - self._last_clean > _CLEAN_INTERVAL:
            self._last_clean = now
            self._cleanup()

    def invalidate(self, username: str) -> None:
        """Удаляет запись (например, если username успешно заняли)."""
        if self._db is None:
            return
        username = username.lstrip("@").strip().lower()
        try:
            with self._db:
                self._db.execute(
                    "DELETE FROM checked WHERE username = ?", (username,)
                )
        except Exception:
            pass

    def stats(self) -> dict[str, int]:
        if self._db is None:
            return {"total": 0, "hits": self._hits, "misses": self._misses}
        try:
            cur = self._db.execute("SELECT COUNT(*) FROM checked")
            total = int(cur.fetchone()[0])
        except Exception:
            total = 0
        return {"total": total, "hits": self._hits, "misses": self._misses}

    def _cleanup(self) -> None:
        if self._db is None:
            return
        cutoff = time.time() - _RETENTION
        try:
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM checked WHERE cached_at < ?", (cutoff,)
                )
            if cur.rowcount:
                logger.debug("Кэш: удалено %d старых записей", cur.rowcount)
        except Exception:
            logger.exception("cache._cleanup упал")

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None