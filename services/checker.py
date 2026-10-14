"""
Мульти-сессионный чекер username'ов через MTProto.

Фишки:
  • Round-Robin между несколькими Telegram-аккаунтами.
  • HTTP pre-check через https://t.me/<name> — быстрый отсев занятых.
  • CheckCache — SQLite-кэш устойчивых статусов (экономит ~30% запросов).
  • Пауза delay_min..delay_max сек ПЕР-СЕССИЯ (а не глобально).
  • FloodWait паркует только ту сессию, на которой он пришёл.
  • Разлогиненная/забаненная сессия паркуется надолго — пул работает
    на остальных, SessionGuard пытается её поднять.
  • Если все сессии в паузе — наверх летит AllSessionsFloodedError.

Статусы (CheckResult):
  free      — можно занять прямо сейчас;
  fragment  — имя существует только на Fragment (за TON), бесплатно не занять;
  taken     — занято;
  invalid   — не проходит формат Telegram;
  error     — проверить не удалось (сеть/неизвестная ошибка).
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
    RPCError,
    UnauthorizedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UsernameOccupiedError,
    UsernamePurchaseAvailableError,
)

from services.check_cache import CheckCache

logger = logging.getLogger(__name__)

CheckResult = Literal["free", "fragment", "taken", "invalid", "error"]

# Статусы, которые имеет смысл класть в кэш (free и error — нет).
_CACHEABLE: frozenset[str] = frozenset({"taken", "invalid", "fragment"})

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

# На сколько парковать сессию при разных сбоях (секунды).
DEAD_SESSION_PARK = 3600      # разлогин/бан — пусть SessionGuard разбирается
NETWORK_ERROR_PARK = 30       # сетевой сбой — короткая пауза, идём на другую


class AllSessionsFloodedError(RuntimeError):
    """Все сессии пула на паузе — проверять некем."""

    def __init__(self, seconds_until_next: int) -> None:
        super().__init__(
            "Все чекер-аккаунты отдыхают от флуда. "
            f"Ближайший освободится через ~{seconds_until_next} сек."
        )
        self.seconds_until_next = seconds_until_next


def is_valid_username_format(username: str) -> bool:
    """Формат Telegram: 5-32 символа, латиница/цифры/_, без «__»."""
    if not 5 <= len(username) <= 32:
        return False
    if "__" in username:
        return False
    return bool(_USERNAME_RE.match(username))


def _verdict_from_html(html: str, username: str) -> Literal["taken", "unknown"]:
    """
    Разбор HTML t.me/<name>.

      • «Telegram: Contact @<username>» — шаблон-заглушка для незанятых
        имён. Возвращаем "unknown" — точный ответ даст MTProto.
      • Любой другой title = страница реального user/channel/group.
        Возвращаем "taken" — экономим MTProto-запрос.
    """
    m = _OG_TITLE_RE.search(html)
    if not m:
        return "unknown"
    title = (m.group(1) or "").strip()
    if not title:
        return "unknown"
    if "telegram: contact @" in title.lower():
        return "unknown"
    return "taken"


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
    """Пул Telethon-клиентов с Round-Robin и умной защитой от флуда."""

    MAX_RETRIES = 6

    def __init__(
        self,
        clients: list[TelegramClient],
        session_names: list[str],
        *,
        concurrency: int = 4,
        flood_extra: int = 3,
        park_threshold: int = 300,
        delay_min: float = 0.8,
        delay_max: float = 1.5,
        http_precheck_enabled: bool = True,
        cache: CheckCache | None = None,
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
        self._cache = cache

        self._stats: dict[str, int] = {
            "free": 0, "fragment": 0, "taken": 0,
            "invalid": 0, "error": 0,
            "http_short_circuit": 0,
            "cache_hits": 0,
            "floods": 0,
        }

    # ----- Lifecycle / HTTP pre-check ---------------------------------------

    async def start(self) -> None:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS
            )

    async def aclose(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

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
            logger.exception("HTTP pre-check @%s: неожиданная ошибка", username)
            return "unknown"
        return _verdict_from_html(html, username)

    # ----- Round-Robin --------------------------------------------------------

    def _next_session(self) -> _SessionState:
        now = time.monotonic()
        n = len(self._sessions)
        for _ in range(n):
            s = self._sessions[self._index]
            self._index = (self._index + 1) % n
            if s.paused_until <= now:
                return s
        raise AllSessionsFloodedError(self.seconds_until_unpark())

    def _park(self, session: _SessionState, seconds: int, *, flood: bool) -> None:
        """Отправляет сессию на паузу. flood=True — считаем как FloodWait."""
        session.paused_until = max(session.paused_until, time.monotonic() + seconds)
        if flood:
            session.times_flooded += 1
            session.last_flood_seconds = seconds
            self._stats["floods"] += 1

    def seconds_until_unpark(self) -> int:
        """Через сколько секунд освободится ближайшая запаркованная сессия."""
        now = time.monotonic()
        return max(0, int(min(s.paused_until for s in self._sessions) - now))

    # Старое имя — для совместимости с кодом, который его ещё зовёт.
    _seconds_until_unpark = seconds_until_unpark

    def has_available_session(self) -> bool:
        now = time.monotonic()
        return any(s.paused_until <= now for s in self._sessions)

    async def _human_delay(self, session: _SessionState) -> None:
        """Пауза delay_min..delay_max сек между вызовами ЭТОЙ сессии."""
        async with session.delay_lock:
            target = random.uniform(self._delay_min, self._delay_max)
            elapsed = time.monotonic() - session.last_call_at
            if elapsed < target:
                await asyncio.sleep(target - elapsed)
            session.last_call_at = time.monotonic()

    def _remember(self, username: str, result: CheckResult) -> None:
        if self._cache is not None and result in _CACHEABLE:
            self._cache.set(username, result)

    # ----- Основной метод ----------------------------------------------------

    async def check(self, username: str) -> CheckResult:
        """
        Проверяет один username.

        Бросает AllSessionsFloodedError, если все сессии на паузе.
        Любые другие сбои превращаются в статус "error" (с логом).
        """
        username = username.lstrip("@").strip().lower()

        if not is_valid_username_format(username):
            self._stats["invalid"] += 1
            return "invalid"

        # --- Кэш: если видели этот username недавно — не тратим запросы ---
        if self._cache is not None:
            cached = self._cache.get(username)
            if cached in _CACHEABLE:
                self._stats["cache_hits"] += 1
                self._stats[cached] += 1
                logger.debug("@%s → %s (кэш)", username, cached)
                return cached  # type: ignore[return-value]

        async with self._sem:
            # --- HTTP pre-check ---
            if self._http_enabled:
                if await self._http_precheck(username) == "taken":
                    self._stats["taken"] += 1
                    self._stats["http_short_circuit"] += 1
                    self._remember(username, "taken")
                    logger.debug("@%s → taken (HTTP)", username)
                    return "taken"

            # --- MTProto с ретраями по сессиям ---
            for _ in range(self.MAX_RETRIES):
                session = self._next_session()
                await self._human_delay(session)

                try:
                    result = await self._check_mtproto(session, username)
                except FloodWaitError as exc:
                    wait = int(exc.seconds) + self._flood_extra
                    self._park(session, wait, flood=True)
                    logger.warning(
                        "🚦 FloodWait %ss на сессии %s (@%s). Паркуем, идём на другую.",
                        wait, session.name, username,
                    )
                    continue
                except UnauthorizedError as exc:
                    # Сессия разлогинена/забанена. Уводим её из ротации надолго.
                    self._park(session, DEAD_SESSION_PARK, flood=False)
                    logger.error(
                        "Сессия %s не авторизована (%s). Паркуем на %d с.",
                        session.name, type(exc).__name__, DEAD_SESSION_PARK,
                    )
                    continue
                except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                    self._park(session, NETWORK_ERROR_PARK, flood=False)
                    logger.warning(
                        "Сетевой сбой на сессии %s (@%s): %s. Пауза %d с.",
                        session.name, username, exc, NETWORK_ERROR_PARK,
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except RPCError as exc:
                    logger.warning(
                        "Проверка @%s (сессия %s): RPC-ошибка %s",
                        username, session.name, type(exc).__name__,
                    )
                    self._stats["error"] += 1
                    return "error"
                except Exception as exc:
                    logger.exception(
                        "Проверка @%s (сессия %s) упала: %s",
                        username, session.name, exc,
                    )
                    self._stats["error"] += 1
                    return "error"

                self._remember(username, result)
                return result

            logger.warning("@%s: исчерпаны попытки (%d)", username, self.MAX_RETRIES)
            self._stats["error"] += 1
            return "error"

    async def _check_mtproto(
        self, session: _SessionState, username: str
    ) -> CheckResult:
        """
        Два шага:
          1. ResolveUsername — если нашёлся объект, имя занято.
          2. CheckUsername — Telegram отвечает, можно ли занять ИМЕННО сейчас:
             True → free, False → недоступно (зарезервировано) → taken,
             UsernamePurchaseAvailableError → fragment.
        """
        try:
            await session.client(
                functions.contacts.ResolveUsernameRequest(username)
            )
            session.calls_made += 1
            self._stats["taken"] += 1
            logger.debug("@%s → taken (сессия %s)", username, session.name)
            return "taken"
        except UsernameNotOccupiedError:
            session.calls_made += 1
        except UsernameInvalidError:
            session.calls_made += 1
            self._stats["invalid"] += 1
            return "invalid"

        # --- Точная проверка: можно ли занять прямо сейчас ---
        try:
            available = await session.client(
                functions.account.CheckUsernameRequest(username=username)
            )
            session.calls_made += 1
        except UsernamePurchaseAvailableError:
            session.calls_made += 1
            self._stats["fragment"] += 1
            logger.debug("@%s → fragment (только за TON)", username)
            return "fragment"
        except UsernameOccupiedError:
            session.calls_made += 1
            self._stats["taken"] += 1
            logger.debug("@%s → occupied (гонка)", username)
            return "taken"
        except UsernameInvalidError:
            session.calls_made += 1
            self._stats["invalid"] += 1
            return "invalid"

        if available:
            self._stats["free"] += 1
            logger.debug("@%s → FREE (сессия %s)", username, session.name)
            return "free"

        # False без исключения — имя недоступно (зарезервировано Telegram).
        self._stats["taken"] += 1
        logger.debug("@%s → недоступно (CheckUsername=False)", username)
        return "taken"

    # ----- Публичные утилиты -------------------------------------------------

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
