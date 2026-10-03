"""
Session Guard — сторож активных Telethon-сессий.

Раз в CHECK_INTERVAL проверяет каждую сессию через is_user_authorized().
Если сессия разлогинена — пытается восстановить через reconnect.
Если не помогает — уведомляет админов и продолжает работать на живых.

Это спасает от сценария «всё нормально в логах, но бот молча не работает».
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ParseMode
from telethon import TelegramClient

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 1800          # 30 минут
ALERT_COOLDOWN = 3600          # не спамить чаще часа об одной и той же сессии


@dataclass
class _SessionState:
    name: str
    client: TelegramClient
    last_alert_at: float = 0.0
    dead_since: float = 0.0
    was_dead: bool = False


class SessionGuard:
    """Фоновая задача, следящая за состоянием Telethon-сессий."""

    def __init__(
        self,
        clients: list[TelegramClient],
        session_names: list[str],
        bot: Bot,
        admin_ids: tuple[int, ...] | frozenset[int],
    ) -> None:
        if len(clients) != len(session_names):
            raise ValueError("clients и session_names должны совпадать")
        self._sessions = [
            _SessionState(name=n, client=c)
            for n, c in zip(session_names, clients)
        ]
        self._bot = bot
        self._admins = tuple(admin_ids)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("Session Guard запущен: %d сессий", len(self._sessions))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        # Даём боту стартовать.
        await asyncio.sleep(60)
        while True:
            try:
                await self._check_all()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Session Guard: ошибка в цикле")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _check_all(self) -> None:
        """Проверяет каждую сессию. Мёртвых — пытается восстановить."""
        for s in self._sessions:
            alive = await self._is_alive(s)
            if alive:
                if s.was_dead:
                    logger.info("Сессия %r восстановлена.", s.name)
                    s.was_dead = False
                    s.dead_since = 0.0
                continue

            # Умерла.
            if not s.was_dead:
                s.was_dead = True
                s.dead_since = time.monotonic()
                logger.warning("Сессия %r не авторизована.", s.name)
                await self._alert_dead(s)
            # Пока не восстановилась — пытаемся что-то сделать.
            if not await self._try_recover(s):
                # Не восстановилась — алерт не чаще раза в час.
                now = time.monotonic()
                if now - s.last_alert_at > ALERT_COOLDOWN:
                    s.last_alert_at = now
                    await self._alert_dead(s)

    async def _is_alive(self, s: _SessionState) -> bool:
        try:
            if not s.client.is_connected():
                await s.client.connect()
            return await s.client.is_user_authorized()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Проверка сессии %r: %s", s.name, exc)
            return False

    async def _try_recover(self, s: _SessionState) -> bool:
        """Пытается переподключиться. Возвращает True, если получилось."""
        try:
            with contextlib.suppress(Exception):
                await s.client.disconnect()
            await asyncio.sleep(2)
            await s.client.connect()
            return await s.client.is_user_authorized()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Recover %r: %s", s.name, exc)
            return False

    async def _alert_dead(self, s: _SessionState) -> None:
        """Уведомляет админов о мёртвой сессии."""
        text = (
            f"<b>SESSION GUARD · ВНИМАНИЕ</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Сессия <code>{s.name}</code> не отвечает.\n"
            f"Причина: разлогинена или заблокирована.\n\n"
            f"<b>Что делать:</b>\n"
            f"  1. Проверьте на телефоне, что аккаунт жив.\n"
            f"  2. Если разлогинили — перезапустите через "
            f"<code>python qr_login.py</code>.\n"
            f"  3. Если аккаунт забанен — уберите его из "
            f"<code>SESSION_NAMES</code> в <code>.env</code>.\n\n"
            f"<i>Остальные сессии продолжают работать.</i>"
        )
        for admin_id in self._admins:
            try:
                await self._bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Session Guard: не доставлено админу %s", admin_id)

    def snapshot(self) -> list[dict[str, object]]:
        """Для /stats — короткая сводка."""
        return [
            {
                "name": s.name,
                "alive": not s.was_dead,
                "dead_since_sec": (
                    int(time.monotonic() - s.dead_since) if s.was_dead else 0
                ),
            }
            for s in self._sessions
        ]   