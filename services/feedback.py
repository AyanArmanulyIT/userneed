"""
FeedbackStorage — хранилище обращений пользователей.

Позволяет:
  • Пользователю написать админам через бота.
  • Админам ответить reply на сообщение — ответ уходит пользователю.
  • Не спамить: не больше N обращений в час на одного пользователя.

Отдельная SQLite-база feedback.db, не смешивается с другими.
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
    user_id: int
    admin_id: int
    admin_chat_id: int
    admin_msg_id: int
    text: str
    status: str
    created_at: float
    answered_at: float


class FeedbackStorage:
    def __init__(self, path: Path | str, retention_days: int = RETENTION_DAYS) -> None:
        self._path = Path(path)
        self._retention_days = max(1, retention_days)
        try:
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
            logger.info("Хранилище обращений: %s", self._path)
        except Exception as exc:
            logger.exception("Не удалось открыть %s: %s", self._path, exc)
            self._db = None  # type: ignore[assignment]

    def _init_schema(self) -> None:
        assert self._db is not None
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id        INTEGER NOT NULL,
                    admin_id       INTEGER NOT NULL,
                    admin_chat_id  INTEGER NOT NULL,
                    admin_msg_id   INTEGER NOT NULL,
                    text           TEXT NOT NULL,
                    status         TEXT NOT NULL DEFAULT 'sent',
                    created_at     REAL NOT NULL,
                    answered_at    REAL NOT NULL DEFAULT 0
                )
            """)
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
        admin_chat_id: int,
        admin_msg_id: int,
        text: str,
    ) -> int:
        if self._db is None:
            return 0
        try:
            with self._db:
                cur = self._db.execute(
                    """
                    INSERT INTO feedback
                        (user_id, admin_id, admin_chat_id, admin_msg_id, text, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, admin_id, admin_chat_id, admin_msg_id, text, time.time()),
                )
            return cur.lastrowid or 0
        except Exception:
            logger.exception("feedback.save упал")
            return 0

    def mark_answered(self, record_id: int) -> None:
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(
                    "UPDATE feedback SET status = 'answered', answered_at = ? WHERE id = ?",
                    (time.time(), record_id),
                )
        except Exception:
            logger.exception("feedback.mark_answered упал")

    # --- Чтение --------------------------------------------------------------

    def find_by_admin_msg(
        self, admin_chat_id: int, admin_msg_id: int
    ) -> FeedbackRecord | None:
        if self._db is None:
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
        except Exception:
            logger.exception("feedback.find_by_admin_msg упал")
            return None

    def count_by_user_last_hour(self, user_id: int) -> int:
        if self._db is None:
            return 0
        cutoff = time.time() - 3600
        try:
            cur = self._db.execute(
                "SELECT COUNT(*) FROM feedback WHERE user_id = ? AND created_at >= ?",
                (user_id, cutoff),
            )
            return int(cur.fetchone()[0])
        except Exception:
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
        except Exception:
            return 0

    def recent(self, limit: int = 20) -> list[FeedbackRecord]:
        if self._db is None:
            return []
        try:
            cur = self._db.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [self._row_to_record(r) for r in cur.fetchall()]
        except Exception:
            return []

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
        except Exception:
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
            user_id=row["user_id"],
            admin_id=row["admin_id"],
            admin_chat_id=row["admin_chat_id"],
            admin_msg_id=row["admin_msg_id"],
            text=row["text"],
            status=row["status"],
            created_at=row["created_at"],
            answered_at=row["answered_at"],
        )   