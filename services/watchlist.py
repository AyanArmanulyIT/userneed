"""
Watchlist — фоновый монитор избранных username'ов (watchlist.db).

Приоритезация (экономит 60-70% MTProto-запросов):
  • hot      — добавлено < 1 дня    → проверять каждый WATCH_HOT_INTERVAL
  • warm     — добавлено < 7 дней   → проверять каждый WATCH_WARM_INTERVAL
  • cold     — добавлено >= 7 дней  → проверять каждый WATCH_COLD_INTERVAL
  • sleeping — status='taken' и не менялся N дней, либо 'invalid' →
               не проверяется (next_check_at = 0)

Ошибки проверки («error», исключения, флуд) НЕ перезаписывают статус
позиции: она остаётся с прежним статусом и уходит на короткий повтор.

Работает параллельно боту — отдельная asyncio.task, отдельная SQLite-база.
Модуль доступен только администраторам (is_admin).
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

from services.checker import AllSessionsFloodedError, TelegramChecker

logger = logging.getLogger(__name__)

# Путь по умолчанию — корень проекта (рядом с config.py). main.py передаёт
# явный путь из конфига; этот нужен только для обратной совместимости.
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "watchlist.db"

RETRY_AFTER_ERROR = 900        # повтор после ошибки проверки, сек
STARTUP_DELAY = 60             # даём боту стартовать
LOOP_INTERVAL = 60             # пауза между волнами
HEARTBEAT_INTERVAL = 3600
MAX_CONSECUTIVE_ERRORS = 5
ERROR_BACKOFF = 600

_LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


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
        path: Path | str | None = None,
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
        self._path = Path(path) if path is not None else _DEFAULT_DB_PATH
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()
        logger.info("Watchlist: база %s", self._path)

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

    @staticmethod
    def _norm(username: str) -> str:
        return username.lstrip("@").strip().lower()

    # --- Управление списком --------------------------------------------------

    def add(self, username: str) -> bool:
        username = self._norm(username)
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
        except sqlite3.Error as exc:
            logger.exception("watchlist.add(%s): %s", username, exc)
            return False

    def remove(self, username: str) -> bool:
        username = self._norm(username)
        if not username:
            return False
        try:
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM targets WHERE username = ?", (username,),
                )
            return cur.rowcount > 0
        except sqlite3.Error as exc:
            logger.exception("watchlist.remove(%s): %s", username, exc)
            return False

    def all_targets(self) -> list[tuple[str, str]]:
        try:
            cur = self._db.execute(
                "SELECT username, status FROM targets ORDER BY username"
            )
            return [(r["username"], r["status"]) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.exception("watchlist.all_targets: %s", exc)
            return []

    def count(self) -> int:
        try:
            cur = self._db.execute("SELECT COUNT(*) FROM targets")
            return int(cur.fetchone()[0])
        except sqlite3.Error as exc:
            logger.exception("watchlist.count: %s", exc)
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
        except sqlite3.Error as exc:
            logger.exception("watchlist.breakdown: %s", exc)
        return out

    # --- Права ---------------------------------------------------------------

    def is_admin(self, user_id: int) -> bool:
        return user_id in self._admins

    # --- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="watchlist")
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
        with contextlib.suppress(sqlite3.Error):
            self._db.close()

    # --- Фоновый цикл --------------------------------------------------------

    async def _loop(self) -> None:
        """
        Фоновый цикл с защитой от падения: ошибка внутри волны логируется,
        задача живёт дальше. Раз в час — heartbeat в лог.
        """
        await asyncio.sleep(STARTUP_DELAY)
        consecutive_errors = 0
        last_heartbeat = time.time()

        while True:
            try:
                await self._scan_once()
                consecutive_errors = 0
            except asyncio.CancelledError:
                logger.info("Watchlist: остановлен")
                raise
            except Exception as exc:  # noqa: BLE001 — сторож не должен умирать
                consecutive_errors += 1
                logger.exception(
                    "Watchlist: ошибка (подряд %d): %s", consecutive_errors, exc,
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error(
                        "Watchlist: %d ошибок подряд — пауза %d с.",
                        MAX_CONSECUTIVE_ERRORS, ERROR_BACKOFF,
                    )
                    await asyncio.sleep(ERROR_BACKOFF)
                    consecutive_errors = 0

            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                last_heartbeat = now
                logger.info("Watchlist heartbeat: %d позиций в списке", self.count())

            await asyncio.sleep(LOOP_INTERVAL)

    def _due_rows(self, now: float) -> list[dict]:
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
        return [dict(r) for r in cur.fetchall()]

    async def _scan_once(self) -> None:
        """Одна волна проверки «созревших» позиций."""
        if not self._checker.has_available_session():
            logger.debug("Watchlist: все сессии на паузе, волну пропускаем")
            return

        try:
            rows = self._due_rows(time.time())
        except sqlite3.Error:
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
            except AllSessionsFloodedError as exc:
                logger.warning(
                    "Watchlist: все сессии во флуде (~%d с) — волна прервана.",
                    exc.seconds_until_next,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Watchlist: @%s — %s", username, exc)
                self._postpone(username, RETRY_AFTER_ERROR)
                continue

            if new_status == "error":
                # Временный сбой: статус не трогаем, повторим позже.
                self._postpone(username, RETRY_AFTER_ERROR)
                continue

            self._update_row(username, new_status)

            if new_status == "free" and old_status != "free":
                await self._notify_free(username)

    # --- Обновление строк ----------------------------------------------------

    def _postpone(self, username: str, seconds: int) -> None:
        """Сдвигает следующую проверку, не меняя статус."""
        now = time.time()
        try:
            with self._db:
                self._db.execute(
                    "UPDATE targets SET last_check = ?, next_check_at = ? "
                    "WHERE username = ?",
                    (now, now + seconds, username),
                )
        except sqlite3.Error:
            logger.exception("Watchlist: _postpone(%s)", username)

    def _update_row(self, username: str, status: str) -> None:
        """
        Записывает новый статус и пересчитывает next_check_at.
        При смене статуса обновляет last_change и сбрасывает флаг уведомления.
        """
        now = time.time()
        try:
            cur = self._db.execute(
                "SELECT added_at, status, last_change, notified "
                "FROM targets WHERE username = ?",
                (username,),
            )
            row = cur.fetchone()
            if row is None:
                return

            changed = status != row["status"]
            last_change = now if changed else row["last_change"]
            notified = 0 if changed else row["notified"]
            next_check_at = self._compute_next_check(
                status, last_change, row["added_at"], now
            )

            with self._db:
                self._db.execute(
                    """
                    UPDATE targets
                    SET status = ?, last_check = ?, last_change = ?,
                        next_check_at = ?, notified = ?
                    WHERE username = ?
                    """,
                    (status, now, last_change, next_check_at, notified, username),
                )
        except sqlite3.Error:
            logger.exception("Watchlist: _update_row(%s)", username)

    def _compute_next_check(
        self,
        status: str,
        last_change: float,
        added_at: float,
        now: float,
    ) -> float:
        """
        Unixtime следующей проверки. 0.0 (никогда) — позиция «уснула»:
          • invalid — формат не изменится;
          • taken, не менявшийся sleep_after_days.
        """
        if status == "invalid":
            return 0.0
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

    # --- Уведомления ---------------------------------------------------------

    async def _notify_free(self, username: str) -> None:
        """Отправляет сигнал всем админам и помечает позицию уведомлённой."""
        text = (
            f"<b>WATCHLIST · ОСВОБОДИЛСЯ ИДЕНТИФИКАТОР</b>\n"
            f"{_LINE}\n\n"
            f"<code>@{username}</code>  —  <b>свободен</b>\n\n"
            f"Успейте занять: <a href=\"https://t.me/{username}\">t.me/{username}</a>"
        )
        for admin_id in self._admins:
            try:
                await self._bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:  # noqa: BLE001 — доставка не должна ронять цикл
                logger.exception("Watchlist: сигнал не доставлен %s", admin_id)

        try:
            with self._db:
                self._db.execute(
                    "UPDATE targets SET notified = 1 WHERE username = ?",
                    (username,),
                )
        except sqlite3.Error:
            logger.exception("Watchlist: не удалось отметить notified (%s)", username)
