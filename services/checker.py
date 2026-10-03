"""
Мульти-сессионный чекер с точным ответом: free / fragment / taken / invalid.

Логика по каждой позиции:
  1. HTTP pre-check (t.me) — отсеивает заведомо занятые за ~200 мс.
  2. ResolveUsername — узнаёт, существует ли пользователь.
  3. Если «свободен» — подтверждаем CheckUsername:
       · UsernameOccupiedError      → taken (гонка ещё кем-то за пару секунд)
       · UsernamePurchaseAvailableError → fragment (только за TON)
       · Успех                      → free  (точно можно занять)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Literal

import aiohttp
from telethon import TelegramClient, functions
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UsernameOccupiedError,
    UsernamePurchaseAvailableError,
)

logger = logging.getLogger(__name__)

CheckResult = Literal["free", "fragment", "taken", "invalid", "error"]

_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{3,30}[a-zA-Z0-9]$")

_OG_TITLE_RE = re.compile(
    r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3)
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class AllSessionsFloodedError(RuntimeError):
    def __init__(self, seconds_until_next: int) -> None:
        super().__init__(
            "Все чекер-аккаунты отдыхают от флуда. "
            f"Ближайший освободится через ~{seconds_until_next} сек."
        )
        self.seconds_until_next = seconds_until_next


def is_valid_username_format(username: str) -> bool:
    if not 5 <= len(username) <= 32:
        return False
    if "__" in username:
        return False
    return bool(_USERNAME_RE.match(username))


def _verdict_from_html(html: str, username: str) -> Literal["taken", "unknown"]:
    m = _OG_TITLE_RE.search(html)
    if not m:
        return "unknown"
    title = (m.group(1) or "").strip().lower()
    if not title:
        return "unknown"
    if "telegram: contact @" in title:
        return "unknown"
    if f"(@{username.lower()})" in title:
        return "taken"
    return "unknown"


@dataclass
class _SessionState:
    name: str
    client: TelegramClient
    paused_until: float = 0.0
    times_flooded: int = 0
    last_flood_seconds: int = 0
    calls_made: int = 0
    last_call_at: float = 0.0
    delay_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TelegramChecker:
    MAX_RETRIES = 6

    def __init__(
        self,
        clients: list[TelegramClient],
        session_names: list[str],
        *,
        concurrency: int = 4,
        flood_extra: int = 3,
        park_threshold: int = 300,
        delay_min: float = 1.5,
        delay_max: float = 3.0,
        http_precheck_enabled: bool = True,
    ) -> None:
        if not clients:
            raise ValueError("Нужен хотя бы один клиент")
        if len(clients) != len(session_names):
            raise ValueError("clients и session_names должны совпадать по длине")

        self._sessions = [
            _SessionState(name=n, client=c)
            for n, c in zip(session_names, clients)
        ]
        self._index = 0
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._flood_extra = max(0, flood_extra)
        self._park_threshold = max(30, park_threshold)
        self._delay_min = max(0.0, delay_min)
        self._delay_max = max(self._delay_min, delay_max)
        self._http_enabled = http_precheck_enabled
        self._http: aiohttp.ClientSession | None = None

        self._stats: dict[str, int] = {
            "free": 0, "fragment": 0, "taken": 0,
            "invalid": 0, "error": 0, "http_short_circuit": 0, "floods": 0,
        }

    # --- Жизненный цикл ------------------------------------------------------

    async def start(self) -> None:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS
            )

    async def aclose(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    # --- HTTP pre-check ------------------------------------------------------

    async def _http_precheck(self, username: str) -> Literal["taken", "unknown"]:
        if self._http is None or self._http.closed:
            await self.start()
        assert self._http is not None
        try:
            async with self._http.get(
                f"https://t.me/{username}", allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return "unknown"
                html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return "unknown"
        except Exception:
            logger.exception("HTTP pre-check @%s", username)
            return "unknown"
        return _verdict_from_html(html, username)

    # --- Round-Robin ---------------------------------------------------------

    async def _next_session(self) -> _SessionState:
        now = time.monotonic()
        n = len(self._sessions)
        for _ in range(n):
            s = self._sessions[self._index]
            self._index = (self._index + 1) % n
            if s.paused_until <= now:
                return s
        raise AllSessionsFloodedError(self._seconds_until_unpark())

    def _park(self, session: _SessionState, seconds: int) -> None:
        session.paused_until = max(session.paused_until, time.monotonic() + seconds)
        session.times_flooded += 1
        session.last_flood_seconds = seconds
        self._stats["floods"] += 1

    def _seconds_until_unpark(self) -> int:
        now = time.monotonic()
        return max(0, int(min(s.paused_until for s in self._sessions) - now))

    async def _human_delay(self, session: _SessionState) -> None:
        async with session.delay_lock:
            target = random.uniform(self._delay_min, self._delay_max)
            elapsed = time.monotonic() - session.last_call_at
            if elapsed < target:
                await asyncio.sleep(target - elapsed)
            session.last_call_at = time.monotonic()

    # --- Основная логика -----------------------------------------------------

    async def check(self, username: str) -> CheckResult:
        """Проверка одной позиции. Может бросить AllSessionsFloodedError."""
        username = username.lstrip("@").strip().lower()

        if not is_valid_username_format(username):
            self._stats["invalid"] += 1
            return "invalid"

        async with self._sem:
            # 1) HTTP pre-check
            if self._http_enabled:
                if await self._http_precheck(username) == "taken":
                    self._stats["taken"] += 1
                    self._stats["http_short_circuit"] += 1
                    return "taken"

            # 2) MTProto с ретраями на разных сессиях
            for _ in range(self.MAX_RETRIES):
                session = await self._next_session()
                await self._human_delay(session)

                try:
                    return await self._check_mtproto(session, username)
                except FloodWaitError as exc:
                    wait = int(exc.seconds) + self._flood_extra
                    self._park(session, wait)
                    logger.warning(
                        "FloodWait %ss на %s (@%s). Переключаюсь.",
                        wait, session.name, username,
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "Проверка @%s (%s) упала: %s",
                        username, session.name, exc,
                    )
                    self._stats["error"] += 1
                    return "error"

            self._stats["error"] += 1
            return "error"

    async def _check_mtproto(
        self, session: _SessionState, username: str
    ) -> CheckResult:
        """Шаг 2 из плана: Resolve → при «свободен» CheckUsername."""
        try:
            # Существует ли вообще?
            await session.client(
                functions.contacts.ResolveUsernameRequest(username)
            )
            session.calls_made += 1
            self._stats["taken"] += 1
            logger.debug("@%s → taken (Resolve)", username)
            return "taken"

        except UsernameNotOccupiedError:
            # Может быть свободен. Подтверждаем точным запросом.
            session.calls_made += 1
            pass

        except UsernameInvalidError:
            self._stats["invalid"] += 1
            return "invalid"

        # --- Точная проверка: можно ли занять прямо сейчас ---
        try:
            await session.client(
                functions.account.CheckUsernameRequest(username=username)
            )
            self._stats["free"] += 1
            logger.debug("@%s → FREE", username)
            return "free"

        except UsernamePurchaseAvailableError:
            self._stats["fragment"] += 1
            logger.debug("@%s → fragment (только за TON)", username)
            return "fragment"

        except UsernameOccupiedError:
            self._stats["taken"] += 1
            logger.debug("@%s → occupied (гонка)", username)
            return "taken"

        except UsernameInvalidError:
            self._stats["invalid"] += 1
            return "invalid"

    # --- Утилиты -------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def clients(self) -> list[TelegramClient]:
        return [s.client for s in self._sessions]

    @property
    def session_names(self) -> list[str]:
        return [s.name for s in self._sessions]

    def sessions_snapshot(self) -> list[dict[str, object]]:
        now = time.monotonic()
        return [
            {
                "name": s.name,
                "paused_for": max(0, int(s.paused_until - now)),
                "calls_made": s.calls_made,
                "times_flooded": s.times_flooded,
            }
            for s in self._sessions
        ]


MultiSessionChecker = TelegramChecker
UsernameChecker = TelegramChecker