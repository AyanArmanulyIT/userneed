"""
DailyReporter — суточная сводка админам.

Раз в сутки бот отправляет админам отчёт: находки, активные пользователи,
дельты счётчиков чекера, состояние сессий, watchlist.

Работает параллельно боту в отдельной asyncio.task. Метка последней
отправки лежит в last_report.txt — при рестарте не отправляет дубликат.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode

from services.checker import TelegramChecker
from services.session_guard import SessionGuard
from services.storage import FoundStorage
from services.watchlist import Watchlist

logger = logging.getLogger(__name__)

REPORT_INTERVAL = 86400              # раз в сутки
LAST_REPORT_FILE = Path("last_report.txt")

_LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


class DailyReporter:
    def __init__(
        self,
        bot: Bot,
        admin_ids: tuple[int, ...] | frozenset[int],
        checker: TelegramChecker,
        storage: FoundStorage,
        watchlist: Watchlist,
        guard: SessionGuard,
    ) -> None:
        self._bot = bot
        self._admins = tuple(admin_ids)
        self._checker = checker
        self._storage = storage
        self._watchlist = watchlist
        self._guard = guard
        self._task: asyncio.Task | None = None
        # Снапшот счётчиков чекера — в отчёте покажем ДЕЛЬТУ за период.
        self._start_snapshot = dict(checker.stats)
        self._period_start = time.time()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "DailyReporter запущен (интервал %d ч)",
                REPORT_INTERVAL // 3600,
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        await asyncio.sleep(120)     # даём боту стартовать
        while True:
            try:
                if await self._should_send():
                    await self.send_now()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("DailyReporter: ошибка в цикле")
            await asyncio.sleep(3600)   # раз в час проверяем, надо ли

    async def _should_send(self) -> bool:
        if not LAST_REPORT_FILE.is_file():
            return True
        try:
            last = float(LAST_REPORT_FILE.read_text().strip())
        except Exception:
            return True
        return (time.time() - last) >= REPORT_INTERVAL

    async def send_now(self) -> None:
        """Формирует и отправляет отчёт админам."""
        logger.info("DailyReporter: отправляю сводку")
        text = self._build_text()
        for admin_id in self._admins:
            try:
                await self._bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception(
                    "DailyReporter: не доставлено админу %s", admin_id
                )
        try:
            LAST_REPORT_FILE.write_text(str(time.time()))
        except Exception:
            logger.exception("DailyReporter: не могу записать метку")

        # Сброс периода: следующая сводка считается от текущего момента.
        self._start_snapshot = dict(self._checker.stats)
        self._period_start = time.time()

    def _build_text(self) -> str:
        now = time.time()
        period_hours = max(1, int((now - self._period_start) / 3600))

        cur = self._checker.stats
        delta = {
            k: cur.get(k, 0) - self._start_snapshot.get(k, 0)
            for k in ("free", "taken", "fragment",
                      "http_short_circuit", "floods")
        }

        records = self._storage.recent(user_id=None, days=1, limit=100000)
        unique_users = len({r.found_by for r in records if r.found_by})

        gsnap = self._guard.snapshot()
        alive = sum(1 for s in gsnap if s["alive"])

        wb = self._watchlist.breakdown()

        lines = [
            "<b>USERNEED · СВОДКА</b>",
            _LINE,
            f"<i>период: {period_hours} ч</i>",
            "",
            "<b>ПОЛЬЗОВАТЕЛИ</b>",
            f"    уникальных:    <b>{unique_users}</b>",
            f"    находок всего: <b>{len(records)}</b>",
            "",
            "<b>ПРОВЕРКИ (дельта за период)</b>",
            f"    свободных:     <b>{delta['free']}</b>",
            f"    за TON:        <b>{delta['fragment']}</b>",
            f"    занятых:       <b>{delta['taken']}</b>",
            f"    HTTP-отсев:    <b>{delta['http_short_circuit']}</b>",
            "",
            "<b>СЕССИИ</b>",
            f"    живых:         <b>{alive} / {len(gsnap)}</b>",
            f"    флудов:        <b>{delta['floods']}</b>",
            "",
            "<b>WATCHLIST</b>",
            f"    позиций:       <b>{wb['total']}</b>",
            f"    свежих:        <b>{wb['hot']}</b>",
        ]
        return "\n".join(lines)             