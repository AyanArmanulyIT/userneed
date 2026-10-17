"""
SQLite-хранилище находок и истории проверок (found.db).

Задачи модуля:
  • Запоминать всё, что бот нашёл свободным — с оценкой и датой.
  • Помнить, какие имена пользователи отметили как занятые («✓ Занял»),
    чтобы генератор больше их не предлагал.
  • Хранить историю для /found и экспорта.
  • Периодически чистить старые записи (retention).
  • Никаких внешних зависимостей — только стандартный sqlite3.

Работает в одном потоке с основным asyncio-loop. Все методы синхронные —
это нормально: SQLite-операции занимают микросекунды.

Все публичные методы не бросают исключений наружу: если БД испортилась,
бот продолжит работать, просто без истории (ошибки уходят в лог).
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
#                                 Модель
# =============================================================================

@dataclass(slots=True)
class FoundRecord:
    """Одна запись о найденном имени."""
    username: str
    score: int
    source: str            # 'smart' | 'random' | 'similar' | 'bulk'
    status: str            # 'free' | 'fragment' | 'taken' | 'invalid' | 'error'
    found_at: float        # unixtime
    found_by: int          # user_id (0 — системное)
    claimed: bool = False
    claimed_at: float = 0.0


# =============================================================================
#                              Хранилище
# =============================================================================

class FoundStorage:
    """Обёртка над SQLite с таблицей находок."""

    def __init__(self, path: Path | str, retention_days: int = 30) -> None:
        self._path = Path(path) if str(path) != ":memory:" else path
        self._retention_days = max(1, retention_days)
        self._db: sqlite3.Connection | None = None

        try:
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
            logger.info("Хранилище находок: %s", self._path)
        except sqlite3.Error as exc:
            logger.exception("Не удалось открыть %s: %s", self._path, exc)
            self._db = None

    # --- Схема ---------------------------------------------------------------

    def _init_schema(self) -> None:
        assert self._db is not None
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS found (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    username    TEXT NOT NULL,
                    score       INTEGER NOT NULL DEFAULT 0,
                    source      TEXT NOT NULL DEFAULT 'unknown',
                    status      TEXT NOT NULL DEFAULT 'free',
                    found_at    REAL NOT NULL,
                    found_by    INTEGER NOT NULL DEFAULT 0,
                    claimed     INTEGER NOT NULL DEFAULT 0,
                    claimed_at  REAL NOT NULL DEFAULT 0
                )
            """)
            # Быстрый поиск «уже находили этот username?»
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_found_username
                    ON found (username)
            """)
            # Быстрый фильтр «свежие находки для пользователя».
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_found_user_time
                    ON found (found_by, found_at DESC)
            """)

    @staticmethod
    def _norm(username: str) -> str:
        return username.lstrip("@").strip().lower()

    # --- Запись --------------------------------------------------------------

    def record(
        self,
        username: str,
        score: int,
        source: str,
        status: str,
        found_by: int = 0,
    ) -> bool:
        """
        Записывает находку. Возвращает True, если имя записано ВПЕРВЫЕ —
        это признак «новая находка», а не дежавю.
        """
        if self._db is None:
            return True     # без БД считаем всё новым, чтобы не ломать UX

        username = self._norm(username)
        if not username:
            return False

        try:
            with self._db:
                cur = self._db.execute(
                    "SELECT 1 FROM found WHERE username = ? LIMIT 1",
                    (username,),
                )
                is_new = cur.fetchone() is None
                self._db.execute(
                    """
                    INSERT INTO found (username, score, source, status, found_at, found_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (username, score, source, status, time.time(), found_by),
                )
            return is_new
        except sqlite3.Error as exc:
            logger.exception("record(%s) упал: %s", username, exc)
            return False

    def mark_claimed(self, username: str, user_id: int | None = None) -> int:
        """
        Отметить имя как занятое пользователем.

        user_id=None — отметить все записи с этим именем (системный вызов).
        user_id=N    — только записи, которые нашёл этот пользователь:
                       чужие находки он пометить не может.
        Возвращает число изменённых строк.
        """
        if self._db is None:
            return 0
        username = self._norm(username)
        if not username:
            return 0
        sql = "UPDATE found SET claimed = 1, claimed_at = ? WHERE username = ?"
        params: list[object] = [time.time(), username]
        if user_id is not None:
            sql += " AND found_by = ?"
            params.append(user_id)
        try:
            with self._db:
                cur = self._db.execute(sql, params)
            return cur.rowcount or 0
        except sqlite3.Error as exc:
            logger.exception("mark_claimed(%s) упал: %s", username, exc)
            return 0

    # --- Чтение --------------------------------------------------------------

    def was_seen_before(self, username: str) -> bool:
        """True — этот username бот уже находил раньше."""
        if self._db is None:
            return False
        try:
            cur = self._db.execute(
                "SELECT 1 FROM found WHERE username = ? LIMIT 1",
                (self._norm(username),),
            )
            return cur.fetchone() is not None
        except sqlite3.Error as exc:
            logger.exception("was_seen_before() упал: %s", exc)
            return False

    def is_claimed(self, username: str) -> bool:
        """True — кто-то уже нажал «✓ Занял» для этого имени."""
        if self._db is None:
            return False
        try:
            cur = self._db.execute(
                "SELECT 1 FROM found WHERE username = ? AND claimed = 1 LIMIT 1",
                (self._norm(username),),
            )
            return cur.fetchone() is not None
        except sqlite3.Error as exc:
            logger.exception("is_claimed() упал: %s", exc)
            return False

    def claimed_names(self) -> set[str]:
        """Все имена, отмеченные как занятые — для фильтра в генераторе."""
        if self._db is None:
            return set()
        try:
            cur = self._db.execute(
                "SELECT DISTINCT username FROM found WHERE claimed = 1"
            )
            return {r["username"] for r in cur.fetchall()}
        except sqlite3.Error as exc:
            logger.exception("claimed_names() упал: %s", exc)
            return set()

    def recent(
        self,
        user_id: int | None = None,
        days: int = 7,
        limit: int = 100,
        only_free: bool = False,
    ) -> list[FoundRecord]:
        """
        Возвращает последние находки.
        user_id=None — по всем пользователям (для админ-команд).
        """
        if self._db is None:
            return []

        cutoff = time.time() - days * 86400
        where = ["found_at >= ?"]
        params: list[object] = [cutoff]

        if user_id is not None:
            where.append("found_by = ?")
            params.append(user_id)
        if only_free:
            where.append("status = 'free'")

        sql = (
            f"SELECT * FROM found WHERE {' AND '.join(where)} "
            f"ORDER BY found_at DESC LIMIT ?"
        )
        params.append(limit)

        try:
            cur = self._db.execute(sql, params)
            return [self._row_to_record(r) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.exception("recent() упал: %s", exc)
            return []

    def count_since(self, days: int = 1) -> tuple[int, int]:
        """(записей, уникальных пользователей) за последние N дней."""
        if self._db is None:
            return 0, 0
        cutoff = time.time() - days * 86400
        try:
            cur = self._db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT found_by) FROM found "
                "WHERE found_at >= ? AND found_by != 0",
                (cutoff,),
            )
            row = cur.fetchone()
            return int(row[0]), int(row[1])
        except sqlite3.Error as exc:
            logger.exception("count_since() упал: %s", exc)
            return 0, 0

    def export_csv(
        self,
        path: Path | str,
        user_id: int | None = None,
        days: int | None = None,
    ) -> int:
        """Экспортирует находки в CSV. Возвращает число строк (без заголовка)."""
        if self._db is None:
            return 0

        where: list[str] = []
        params: list[object] = []
        if user_id is not None:
            where.append("found_by = ?")
            params.append(user_id)
        if days is not None:
            where.append("found_at >= ?")
            params.append(time.time() - days * 86400)

        sql = "SELECT * FROM found"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY found_at DESC"

        try:
            cur = self._db.execute(sql, params)
            rows = cur.fetchall()
        except sqlite3.Error as exc:
            logger.exception("export_csv() выборка упала: %s", exc)
            return 0

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow([
                    "username", "score", "status", "source",
                    "found_at", "found_by", "claimed",
                ])
                for r in rows:
                    writer.writerow([
                        r["username"],
                        r["score"],
                        r["status"],
                        r["source"],
                        time.strftime(
                            "%Y-%m-%d %H:%M",
                            time.localtime(r["found_at"]),
                        ),
                        r["found_by"],
                        "yes" if r["claimed"] else "no",
                    ])
            return len(rows)
        except OSError as exc:
            logger.exception("export_csv(%s) запись упала: %s", path, exc)
            return 0

    # --- Обслуживание --------------------------------------------------------

    def cleanup(self) -> int:
        """
        Удаляет записи старше retention_days. Записи с пометкой «занято»
        не трогаем — они нужны, чтобы имя не предлагалось снова.
        Возвращает число удалённых строк.
        """
        if self._db is None:
            return 0
        cutoff = time.time() - self._retention_days * 86400
        try:
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM found WHERE found_at < ? AND claimed = 0",
                    (cutoff,),
                )
            removed = cur.rowcount or 0
            if removed:
                logger.info(
                    "Хранилище: удалено %d старых записей (старше %d дней).",
                    removed, self._retention_days,
                )
            return removed
        except sqlite3.Error as exc:
            logger.exception("cleanup() упал: %s", exc)
            return 0

    def stats(self) -> dict[str, int]:
        """Сводные счётчики для /stats."""
        empty = {"total": 0, "free": 0, "claimed": 0}
        if self._db is None:
            return empty
        try:
            cur = self._db.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status = 'free' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN claimed = 1 THEN 1 ELSE 0 END) "
                "FROM found"
            )
            row = cur.fetchone()
            return {
                "total": int(row[0] or 0),
                "free": int(row[1] or 0),
                "claimed": int(row[2] or 0),
            }
        except sqlite3.Error as exc:
            logger.exception("stats() упал: %s", exc)
            return empty

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error as exc:
                logger.debug("close(): %s", exc)
            self._db = None

    # --- Внутренние ----------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> FoundRecord:
        return FoundRecord(
            username=row["username"],
            score=row["score"],
            source=row["source"],
            status=row["status"],
            found_at=row["found_at"],
            found_by=row["found_by"],
            claimed=bool(row["claimed"]),
            claimed_at=row["claimed_at"],
        )
