"""
Watchlist — фоновый монитор избранных username'ов.

Приоритезация (экономит 60-70% MTProto-запросов):
  • hot   — добавлено < 1 дня    → проверять каждый WATCH_HOT_INTERVAL
  • warm  — добавлено < 7 дней   → проверять каждый WATCH_WARM_INTERVAL
  • cold  — добавлено >= 7 дней  → проверять каждый WATCH_COLD_INTERVAL
  • sleeping — status='taken' и не менялся N дней → не проверяется

Работает параллельно боту — отдельная asyncio.task, отдельная SQLite-база.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode

from services.checker import TelegramChecker

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "watchlist.db"


# =============================================================================
#                                 Модель
# =============================================================================

@dataclass(slots=True)
class WatchEntry:
    username: str
    status: str
    added_at: float
    last_check: float
    last_change: float
    next_check_at: float


# =============================================================================
#                              Watchlist
# =============================================================================

class Watchlist:
    """
    Фоновый монитор. Публичный интерфейс:

      add(username)          → bool
      remove(username)       → bool
      all_targets()          → list[(username, status)]
      count()                → int
      breakdown()            → {hot, warm, cold, sleeping, total}
      is_admin(user_id)      → bool
      start() / stop()       — lifecycle
    """

    def __init__(
        self,
        checker: TelegramChecker,
        bot: Bot,
        admin_ids: tuple[int, ...] | frozenset[int],
        *,
        hot_interval: int = 3600,
        warm_interval: int = 14400,
        cold_interval: int = 86400,
        batch_size: int = 30,
        sleep_after_days: int = 20,
    ) -> None:
        self._checker = checker
        self._bot = bot
        self._admins = frozenset(admin_ids)

        self._hot = max(300, hot_interval)
        self._warm = max(self._hot, warm_interval)
        self._cold = max(self._warm, cold_interval)
        self._batch = max(5, batch_size)
        self._sleep_after = max(1, sleep_after_days)

        self._task: asyncio.Task | None = None
        self._db = sqlite3.connect(_DB_PATH, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    # --- Схема ---------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS targets (
                    username       TEXT PRIMARY KEY,
                    status         TEXT DEFAULT 'pending',
                    added_at       REAL DEFAULT 0,
                    last_check     REAL DEFAULT 0,
                    last_change    REAL DEFAULT 0,
                    next_check_at  REAL DEFAULT 0,
                    notified       INTEGER DEFAULT 0
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_targets_next
                    ON targets (next_check_at)
            """)

    # --- Управление списком --------------------------------------------------

    def add(self, username: str) -> bool:
        username = username.lstrip("@").strip().lower()
        if not username:
            return False
        now = time.time()
        try:
            with self._db:
                cur = self._db.execute(
                    """
                    INSERT OR IGNORE INTO targets
                        (username, added_at, next_check_at)
                    VALUES (?, ?, ?)
                    """,
                    (username, now, now),
                )
            return cur.rowcount > 0
        except Exception as exc:
            logger.exception("watchlist.add(%s): %s", username, exc)
            return False

    def remove(self, username: str) -> bool:
        username = username.lstrip("@").strip().lower()
        try:
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM targets WHERE username = ?", (username,),
                )
            return cur.rowcount > 0
        except Exception:
            return False

    def all_targets(self) -> list[tuple[str, str]]:
        try:
            cur = self._db.execute(
                "SELECT username, status FROM targets ORDER BY username"
            )
            return [(r["username"], r["status"]) for r in cur.fetchall()]
        except Exception:
            return []

    def count(self) -> int:
        try:
            cur = self._db.execute("SELECT COUNT(*) FROM targets")
            return int(cur.fetchone()[0])
        except Exception:
            return 0

    def breakdown(self) -> dict[str, int]:
        """
        {hot: N, warm: M, cold: K, sleeping: S, total: T}.
        'sleeping' — те, у кого next_check_at == 0 (уснули).
        """
        now = time.time()
        out = {"hot": 0, "warm": 0, "cold": 0, "sleeping": 0, "total": 0}
        try:
            cur = self._db.execute(
                "SELECT added_at, status, last_change, next_check_at FROM targets"
            )
            for r in cur.fetchall():
                out["total"] += 1
                if r["next_check_at"] == 0:
                    out["sleeping"] += 1
                    continue
                age_days = (now - r["added_at"]) / 86400
                if age_days < 1:
                    out["hot"] += 1
                elif age_days < 7:
                    out["warm"] += 1
                else:
                    out["cold"] += 1
        except Exception:
            pass
        return out

    # --- Права ---------------------------------------------------------------

    def is_admin(self, user_id: int) -> bool:
        return user_id in self._admins

    # --- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            b = self.breakdown()
            logger.info(
                "Watchlist запущен: всего %d позиций (hot=%d, warm=%d, "
                "cold=%d, sleeping=%d)",
                b["total"], b["hot"], b["warm"], b["cold"], b["sleeping"],
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        try:
            self._db.close()
        except Exception:
            pass

    # --- Фоновый цикл --------------------------------------------------------

    async def _loop(self) -> None:
        """
        Фоновый цикл с защитой от падения.

        Если что-то упадёт внутри _scan_once — не убиваем задачу,
        а логируем и продолжаем. Каждый час пишем heartbeat в лог,
        чтобы было видно, что таск жив.
        """
        await asyncio.sleep(60)   # даём боту стартовать
        consecutive_errors = 0
        last_heartbeat = time.time()

        while True:
            try:
                await self._scan_once()
                consecutive_errors = 0
            except asyncio.CancelledError:
                logger.info("Watchlist: остановлен")
                return
            except Exception as exc:
                consecutive_errors += 1
                logger.exception(
                    "Watchlist: ошибка (подряд %d): %s",
                    consecutive_errors, exc,
                )
                if consecutive_errors >= 5:
                    logger.error(
                        "Watchlist: 5 ошибок подряд — пауза 10 минут."
                    )
                    await asyncio.sleep(600)
                    consecutive_errors = 0

            # Раз в час — heartbeat, чтобы видеть, что таск жив.
            now = time.time()
            if now - last_heartbeat >= 3600:
                last_heartbeat = now
                try:
                    cnt = self.count()
                    logger.info("Watchlist heartbeat: %d позиций в списке", cnt)
                except Exception:
                    logger.exception("Watchlist: heartbeat failed")

            await asyncio.sleep(60)

    async def _scan_once(self) -> None:
        """Одна волна проверки "созревших" позиций."""
        now = time.time()
        try:
            cur = self._db.execute(
                """
                SELECT username, status, added_at, last_check, last_change
                FROM targets
                WHERE next_check_at > 0 AND next_check_at <= ?
                ORDER BY next_check_at ASC
                LIMIT ?
                """,
                (now, self._batch),
            )
            rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            logger.exception("Watchlist: ошибка выборки")
            return

        if not rows:
            return

        logger.info("Watchlist: волна на %d позиций", len(rows))
        for row in rows:
            username = row["username"]
            old_status = row["status"] or "pending"

            try:
                new_status = await self._checker.check(username)
            except Exception as exc:      # noqa: BLE001
                logger.warning("Watchlist: @%s — %s", username, exc)
                self._update_row(
                    username,
                    old_status,
                    next_check_at=time.time() + 900,
                )
                continue

            self._update_row(username, new_status)

            if new_status == "free" and old_status != "free":
                await self._notify_free(username)

    def _update_row(
        self,
        username: str,
        status: str,
        next_check_at: float | None = None,
    ) -> None:
        """
        Обновляет строку.
        Если next_check_at=None — пересчитывает из правил приоритезации.
        Если status сменился — сбрасывает last_change и уведомление.
        """
        now = time.time()
        try:
            cur = self._db.execute(
                "SELECT added_at, status, last_change FROM targets WHERE username = ?",
                (username,),
            )
            row = cur.fetchone()
            if row is None:
                return
            added_at = row["added_at"]
            old_status = row["status"]
            last_change = row["last_change"]

            if status != old_status:
                last_change = now
                notified_reset = "notified = 0,"
            else:
                notified_reset = ""

            if next_check_at is None:
                next_check_at = self._compute_next_check(
                    status, last_change, added_at, now
                )
                if status != "free":
                    notified_reset = "notified = 0,"

            with self._db:
                self._db.execute(
                    f"""
                    UPDATE targets
                    SET status = ?, last_check = ?, last_change = ?,
                        next_check_at = ?, {notified_reset} username = username
                    WHERE username = ?
                    """,
                    (status, now, last_change, next_check_at, username),
                )
        except Exception:
            logger.exception("Watchlist: _update_row(%s)", username)

    def _compute_next_check(
        self,
        status: str,
        last_change: float,
        added_at: float,
        now: float,
    ) -> float:
        """
        Возвращает unixtime следующей проверки.
        0.0 (никогда) — если позиция ушла «спать»:
        статус taken и не менялся sleep_after_days.
        """
        if status == "taken":
            quiet_days = (now - last_change) / 86400 if last_change else 0
            if quiet_days > self._sleep_after:
                return 0.0

        age_days = (now - added_at) / 86400
        if age_days < 1:
            interval = self._hot
        elif age_days < 7:
            interval = self._warm
        else:
            interval = self._cold

        return now + interval

    async def _notify_free(self, username: str) -> None:
        """Отправляет сигнал всем админам."""
        text = (
            f"<b>WATCHLIST · ОСВОБОДИЛСЯ ИДЕНТИФИКАТОР</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>@{username}</code>  —  <b>свободен</b>\n\n"
            f"Успейте занять: <a href=\"https://t.me/{username}\">t.me/{username}</a>"
        )
        for admin_id in self._admins:
            try:
                await self._bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Watchlist: сигнал не доставлен %s", admin_id)

        try:
            with self._db:
                self._db.execute(
                    "UPDATE watchlist SET notified = 1 WHERE username = ?",
                    (username,),
                )
        except Exception:
            # Имя таблицы — targets, оставим совместимость:
            try:
                with self._db:
                    self._db.execute(
                        "UPDATE targets SET notified = 1 WHERE username = ?",
                        (username,),
                    )
            except Exception:
                pass