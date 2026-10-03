"""
SQLite-хранилище находок и истории проверок.

Задачи модуля:
  • Запоминать всё, что бот нашёл свободным — с оценкой и датой.
  • Не показывать «новинку», если имя уже находилось раньше.
  • Хранить историю для /found и экспорта.
  • Периодически чистить старые записи (retention).
  • Никаких внешних зависимостей — только стандартный sqlite3.

Работает в одном потоке с основным asyncio-loop. Все методы синхронные —
это нормально: SQLite-операции занимают микросекунды, при желании можно
вынести в executor, но для нагрузки бота это избыточно.
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
    """
    Обёртка над SQLite с таблицей находок.

    Все публичные методы идемпотентны и не бросают исключений наружу —
    если БД испортилась, бот продолжит работать, просто без истории.
    """

    def __init__(self, path: Path | str, retention_days: int = 30) -> None:
        self._path = Path(path)
        self._retention_days = max(1, retention_days)

        try:
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
            logger.info("Хранилище находок: %s", self._path)
        except Exception as exc:      # noqa: BLE001
            logger.exception("Не удалось открыть %s: %s", self._path, exc)
            self._db = None  # type: ignore[assignment]

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
        Пытается записать находку. Возвращает True, если имя записано
        ВПЕРВЫЕ — это и есть признак «новая находка», а не дежавю.
        """
        if self._db is None:
            return True     # без БД считаем всё новым, чтобы не ломать UX

        username = username.lstrip("@").strip().lower()
        if not username:
            return False

        try:
            with self._db:
                # Проверяем — может, это имя уже светилось?
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
        except Exception as exc:      # noqa: BLE001
            logger.exception("record(%s) упал: %s", username, exc)
            return False

    def mark_claimed(self, username: str) -> None:
        """Отметить имя как занятое пользователем (для статистики)."""
        if self._db is None:
            return
        username = username.lstrip("@").strip().lower()
        try:
            with self._db:
                self._db.execute(
                    "UPDATE found SET claimed = 1, claimed_at = ? "
                    "WHERE username = ?",
                    (time.time(), username),
                )
        except Exception:
            logger.exception("mark_claimed(%s) упал", username)

    # --- Чтение --------------------------------------------------------------

    def was_seen_before(self, username: str) -> bool:
        """True — этот username бот уже находил раньше."""
        if self._db is None:
            return False
        username = username.lstrip("@").strip().lower()
        try:
            cur = self._db.execute(
                "SELECT 1 FROM found WHERE username = ? LIMIT 1",
                (username,),
            )
            return cur.fetchone() is not None
        except Exception:
            return False

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
        except Exception as exc:
            logger.exception("recent() упал: %s", exc)
            return []

    def export_csv(
        self,
        path: Path | str,
        user_id: int | None = None,
        days: int | None = None,
    ) -> int:
        """
        Экспортирует находки в CSV. Возвращает количество строк (без заголовка).
        """
        if self._db is None:
            return 0

        where = []
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
        except Exception as exc:
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
        except Exception as exc:
            logger.exception("export_csv(%s) запись упала: %s", path, exc)
            return 0

    # --- Обслуживание --------------------------------------------------------

    def cleanup(self) -> int:
        """
        Удаляет записи старше retention_days.
        Возвращает число удалённых строк.
        """
        if self._db is None:
            return 0
        cutoff = time.time() - self._retention_days * 86400
        try:
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM found WHERE found_at < ?", (cutoff,)
                )
            removed = cur.rowcount or 0
            if removed:
                logger.info(
                    "Хранилище: удалено %d старых записей (старше %d дней).",
                    removed, self._retention_days,
                )
            return removed
        except Exception as exc:
            logger.exception("cleanup() упал: %s", exc)
            return 0

    def stats(self) -> dict[str, int]:
        """Сводные счётчики для /stats."""
        if self._db is None:
            return {"total": 0, "free": 0, "claimed": 0}
        try:
            cur = self._db.execute("SELECT COUNT(*) FROM found")
            total = int(cur.fetchone()[0])
            cur = self._db.execute(
                "SELECT COUNT(*) FROM found WHERE status = 'free'"
            )
            free = int(cur.fetchone()[0])
            cur = self._db.execute(
                "SELECT COUNT(*) FROM found WHERE claimed = 1"
            )
            claimed = int(cur.fetchone()[0])
            return {"total": total, "free": free, "claimed": claimed}
        except Exception:
            return {"total": 0, "free": 0, "claimed": 0}

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
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