"""
FeedbackStorage — хранилище обращений пользователей (feedback.db).

Позволяет:
  • Пользователю написать админам через бота.
  • Админам ответить reply на сообщение — ответ уходит пользователю.
  • Не спамить: не больше N обращений в час на одного пользователя.

Модель: одно обращение пользователя = один ticket_id. Если админов
несколько, каждому уходит своя копия — это отдельные строки с общим
ticket_id, чтобы номер обращения у пользователя и у всех админов совпадал.

Порядок работы (см. handlers):
  1. save()           — создаём строку ДО отправки, получаем id;
  2. send_message()   — отправляем админу, показывая ticket_id в шапке;
  3. set_admin_msg()  — запоминаем chat_id/message_id для поиска по reply,
     либо mark_failed(), если доставить не удалось.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_PER_HOUR = 3
RETENTION_DAYS = 90


@dataclass(slots=True)
class FeedbackRecord:
    id: int
    ticket_id: int
    user_id: int
    admin_id: int
    admin_chat_id: int
    admin_msg_id: int
    text: str
    status: str            # 'pending' | 'sent' | 'answered' | 'failed'
    created_at: float
    answered_at: float


class FeedbackStorage:
    def __init__(self, path: Path | str, retention_days: int = RETENTION_DAYS) -> None:
        self._path = Path(path)
        self._retention_days = max(1, retention_days)
        self._db: sqlite3.Connection | None = None
        try:
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
            logger.info("Хранилище обращений: %s", self._path)
        except sqlite3.Error as exc:
            logger.exception("Не удалось открыть %s: %s", self._path, exc)
            self._db = None

    def _init_schema(self) -> None:
        assert self._db is not None
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id      INTEGER NOT NULL DEFAULT 0,
                    user_id        INTEGER NOT NULL,
                    admin_id       INTEGER NOT NULL,
                    admin_chat_id  INTEGER NOT NULL DEFAULT 0,
                    admin_msg_id   INTEGER NOT NULL DEFAULT 0,
                    text           TEXT NOT NULL,
                    status         TEXT NOT NULL DEFAULT 'sent',
                    created_at     REAL NOT NULL,
                    answered_at    REAL NOT NULL DEFAULT 0
                )
            """)
            # Миграция старой схемы (без ticket_id): добавляем колонку
            # и считаем, что каждая старая строка — отдельный тикет.
            cols = {
                r["name"] for r in self._db.execute("PRAGMA table_info(feedback)")
            }
            if "ticket_id" not in cols:
                self._db.execute(
                    "ALTER TABLE feedback ADD COLUMN ticket_id INTEGER NOT NULL DEFAULT 0"
                )
            self._db.execute(
                "UPDATE feedback SET ticket_id = id WHERE ticket_id = 0"
            )
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_admin_msg
                    ON feedback (admin_chat_id, admin_msg_id)
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_user_time
                    ON feedback (user_id, created_at DESC)
            """)

    # --- Запись --------------------------------------------------------------

    def save(
        self,
        user_id: int,
        admin_id: int,
        text: str,
        ticket_id: int | None = None,
        admin_chat_id: int = 0,
        admin_msg_id: int = 0,
    ) -> int:
        """
        Создаёт строку обращения. Возвращает её id (0 — если БД недоступна).

        ticket_id=None — это первая копия обращения: номером тикета
        становится её собственный id. Для копий другим админам передайте
        ticket_id первой строки.
        """
        if self._db is None:
            return 0
        status = "sent" if admin_msg_id else "pending"
        try:
            with self._db:
                cur = self._db.execute(
                    """
                    INSERT INTO feedback
                        (ticket_id, user_id, admin_id, admin_chat_id,
                         admin_msg_id, text, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id or 0, user_id, admin_id, admin_chat_id,
                        admin_msg_id, text, status, time.time(),
                    ),
                )
                row_id = int(cur.lastrowid or 0)
                if not ticket_id and row_id:
                    self._db.execute(
                        "UPDATE feedback SET ticket_id = ? WHERE id = ?",
                        (row_id, row_id),
                    )
            return row_id
        except sqlite3.Error as exc:
            logger.exception("feedback.save упал: %s", exc)
            return 0

    def set_admin_msg(self, record_id: int, admin_chat_id: int, admin_msg_id: int) -> None:
        """Запоминает, каким сообщением обращение ушло админу."""
        if self._db is None or not record_id:
            return
        try:
            with self._db:
                self._db.execute(
                    "UPDATE feedback SET admin_chat_id = ?, admin_msg_id = ?, "
                    "status = 'sent' WHERE id = ?",
                    (admin_chat_id, admin_msg_id, record_id),
                )
        except sqlite3.Error as exc:
            logger.exception("feedback.set_admin_msg упал: %s", exc)

    def mark_failed(self, record_id: int) -> None:
        if self._db is None or not record_id:
            return
        try:
            with self._db:
                self._db.execute(
                    "UPDATE feedback SET status = 'failed' WHERE id = ?",
                    (record_id,),
                )
        except sqlite3.Error as exc:
            logger.exception("feedback.mark_failed упал: %s", exc)

    def mark_answered(self, ticket_id: int) -> None:
        """Отмечает отвеченными все копии обращения (у всех админов)."""
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(
                    "UPDATE feedback SET status = 'answered', answered_at = ? "
                    "WHERE ticket_id = ? AND status != 'failed'",
                    (time.time(), ticket_id),
                )
        except sqlite3.Error as exc:
            logger.exception("feedback.mark_answered упал: %s", exc)

    # --- Чтение --------------------------------------------------------------

    def find_by_admin_msg(
        self, admin_chat_id: int, admin_msg_id: int
    ) -> FeedbackRecord | None:
        if self._db is None or not admin_msg_id:
            return None
        try:
            cur = self._db.execute(
                """
                SELECT * FROM feedback
                WHERE admin_chat_id = ? AND admin_msg_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (admin_chat_id, admin_msg_id),
            )
            row = cur.fetchone()
            return self._row_to_record(row) if row else None
        except sqlite3.Error as exc:
            logger.exception("feedback.find_by_admin_msg упал: %s", exc)
            return None

    def count_by_user_last_hour(self, user_id: int) -> int:
        """Число ОБРАЩЕНИЙ (тикетов) пользователя за последний час."""
        if self._db is None:
            return 0
        cutoff = time.time() - 3600
        try:
            cur = self._db.execute(
                "SELECT COUNT(DISTINCT ticket_id) FROM feedback "
                "WHERE user_id = ? AND created_at >= ?",
                (user_id, cutoff),
            )
            return int(cur.fetchone()[0])
        except sqlite3.Error as exc:
            logger.exception("feedback.count_by_user_last_hour упал: %s", exc)
            return 0

    def rating_ok(self, user_id: int, max_per_hour: int = MAX_PER_HOUR) -> bool:
        return self.count_by_user_last_hour(user_id) < max_per_hour

    def seconds_until_next(self, user_id: int) -> int:
        """Сколько секунд до освобождения лимита."""
        if self._db is None:
            return 0
        cutoff = time.time() - 3600
        try:
            cur = self._db.execute(
                """
                SELECT MIN(created_at) FROM feedback
                WHERE user_id = ? AND created_at >= ?
                """,
                (user_id, cutoff),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return 0
            return max(0, int((row[0] + 3600) - time.time()))
        except sqlite3.Error as exc:
            logger.exception("feedback.seconds_until_next упал: %s", exc)
            return 0

    def recent(self, limit: int = 20) -> list[FeedbackRecord]:
        """Последние тикеты — по одной строке на обращение."""
        if self._db is None:
            return []
        try:
            cur = self._db.execute(
                """
                SELECT * FROM feedback
                WHERE id IN (SELECT MIN(id) FROM feedback GROUP BY ticket_id)
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            )
            return [self._row_to_record(r) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.exception("feedback.recent упал: %s", exc)
            return []

    def count(self) -> int:
        """Общее число обращений (тикетов)."""
        if self._db is None:
            return 0
        try:
            cur = self._db.execute("SELECT COUNT(DISTINCT ticket_id) FROM feedback")
            return int(cur.fetchone()[0])
        except sqlite3.Error as exc:
            logger.exception("feedback.count упал: %s", exc)
            return 0

    def cleanup(self) -> int:
        if self._db is None:
            return 0
        cutoff = time.time() - self._retention_days * 86400
        try:
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM feedback WHERE created_at < ?", (cutoff,)
                )
            return cur.rowcount or 0
        except sqlite3.Error as exc:
            logger.exception("feedback.cleanup упал: %s", exc)
            return 0

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> FeedbackRecord:
        return FeedbackRecord(
            id=row["id"],
            ticket_id=row["ticket_id"],
            user_id=row["user_id"],
            admin_id=row["admin_id"],
            admin_chat_id=row["admin_chat_id"],
            admin_msg_id=row["admin_msg_id"],
            text=row["text"],
            status=row["status"],
            created_at=row["created_at"],
            answered_at=row["answered_at"],
        )
