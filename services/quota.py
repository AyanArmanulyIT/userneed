"""
QuotaStorage — учёт сеансов пользователей (quota.db).

Раньше счётчик жил в памяти процесса и обнулялся при каждом рестарте —
лимит фактически не работал. Теперь использованные сеансы лежат в SQLite
и переживают перезапуск.

Правила списания (реализованы в handlers, здесь только учёт):
  • сеанс списывается ПОСЛЕ успешного завершения обработки
    (в том числе если пользователь остановил её сам);
  • при отказе из-за занятости системы, флуда всех сессий, таймаута
    или внутренней ошибки сеанс НЕ списывается.

Администраторы в учёте не участвуют (проверка — на стороне handlers).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class QuotaStorage:
    def __init__(self, path: Path | str, limit: int) -> None:
        self._path = Path(path)
        self._limit = max(1, limit)
        self._db: sqlite3.Connection | None = None
        try:
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
            logger.info("Хранилище квот: %s (лимит %d)", self._path, self._limit)
        except sqlite3.Error as exc:
            logger.exception("Не удалось открыть %s: %s", self._path, exc)
            self._db = None

    def _init_schema(self) -> None:
        assert self._db is not None
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    user_id   INTEGER PRIMARY KEY,
                    used      INTEGER NOT NULL DEFAULT 0,
                    bonus     INTEGER NOT NULL DEFAULT 0,
                    first_at  REAL NOT NULL DEFAULT 0,
                    last_at   REAL NOT NULL DEFAULT 0
                )
            """)

    # --- Чтение --------------------------------------------------------------

    @property
    def limit(self) -> int:
        return self._limit

    def _row(self, user_id: int) -> sqlite3.Row | None:
        if self._db is None:
            return None
        try:
            cur = self._db.execute(
                "SELECT used, bonus, first_at, last_at FROM usage WHERE user_id = ?",
                (user_id,),
            )
            return cur.fetchone()
        except sqlite3.Error as exc:
            logger.exception("quota._row(%s) упал: %s", user_id, exc)
            return None

    def used(self, user_id: int) -> int:
        row = self._row(user_id)
        return int(row["used"]) if row else 0

    def allowed(self, user_id: int) -> int:
        """Лимит пользователя с учётом бонусных сеансов."""
        row = self._row(user_id)
        bonus = int(row["bonus"]) if row else 0
        return self._limit + bonus

    def remaining(self, user_id: int) -> int:
        row = self._row(user_id)
        if row is None:
            return self._limit
        return max(0, self._limit + int(row["bonus"]) - int(row["used"]))

    def is_new(self, user_id: int) -> bool:
        """Пользователь ещё ни разу не завершал обработку."""
        return self.used(user_id) == 0

    # --- Запись --------------------------------------------------------------

    def consume(self, user_id: int) -> int:
        """Списывает один сеанс. Возвращает остаток после списания."""
        if self._db is None:
            return self._limit
        now = time.time()
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO usage (user_id, used, first_at, last_at)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(user_id)
                    DO UPDATE SET used = used + 1, last_at = excluded.last_at
                    """,
                    (user_id, now, now),
                )
        except sqlite3.Error as exc:
            logger.exception("quota.consume(%s) упал: %s", user_id, exc)
        return self.remaining(user_id)

    def reset(self, user_id: int) -> None:
        """Обнуляет использованные сеансы (бонус сохраняется)."""
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(
                    "UPDATE usage SET used = 0 WHERE user_id = ?", (user_id,)
                )
        except sqlite3.Error as exc:
            logger.exception("quota.reset(%s) упал: %s", user_id, exc)

    def grant(self, user_id: int, extra: int) -> int:
        """Добавляет бонусные сеансы сверх лимита. Возвращает новый остаток."""
        if self._db is None:
            return self._limit
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO usage (user_id, used, bonus, first_at, last_at)
                    VALUES (?, 0, ?, 0, 0)
                    ON CONFLICT(user_id)
                    DO UPDATE SET bonus = bonus + excluded.bonus
                    """,
                    (user_id, max(0, extra)),
                )
        except sqlite3.Error as exc:
            logger.exception("quota.grant(%s) упал: %s", user_id, exc)
        return self.remaining(user_id)

    # --- Сводка --------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """{users: сколько пользователей что-то запускали, runs: всего сеансов}."""
        if self._db is None:
            return {"users": 0, "runs": 0}
        try:
            cur = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(used), 0) FROM usage WHERE used > 0"
            )
            row = cur.fetchone()
            return {"users": int(row[0]), "runs": int(row[1])}
        except sqlite3.Error as exc:
            logger.exception("quota.stats упал: %s", exc)
            return {"users": 0, "runs": 0}

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None
