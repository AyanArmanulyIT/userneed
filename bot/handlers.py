"""
Хендлеры aiogram 3.x для системы UserNeed.

Модули:
  01 — Генерация (smart / random / similar)
  02 — Пакетная проверка
  03 — Мониторинг (watchlist, только операторы)
  04 — История находок (SQLite)
  05 — Регламент
  06 — Связь с админом (обратная связь)

Ключевые механики:
  • Закрытая бета: AccessMiddleware (outer) пропускает только whitelist.
  • Один прогон на пользователя + глобальный семафор на всю систему —
    генерация и пакетная проверка идут через общую обвязку _start_run.
  • Жёсткий таймаут прогона (GEN_HARD_TIMEOUT) — семафор не застрянет.
  • Квота хранится в SQLite (QuotaStorage) и списывается ПОСЛЕ успешного
    завершения прогона. Отказ в очереди, флуд всех сессий, таймаут и
    внутренняя ошибка сеанс не списывают.
  • Кнопка «Прервать/Завершить» — per-run asyncio.Event, не глобальный флаг.
  • Все тексты — в bot/branding.py. Здесь строк для пользователя нет.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from bot import branding as B
from bot.keyboards import (
    back_to_main,
    bulk_progress_kb,
    cancel_menu,
    found_actions_kb,
    found_menu,
    gen_count_menu,
    gen_mode_menu,
    gen_progress_kb,
    gen_similar_count_menu,
    main_menu,
    watch_menu,
)
from bot.utils import chunk_lines, progress_bar
from config import Config
from services.backup_manager import BackupManager
from services.check_cache import CheckCache
from services.checker import (
    AllSessionsFloodedError,
    CheckResult,
    TelegramChecker,
    is_valid_username_format,
)
from services.daily_report import DailyReporter
from services.feedback import FeedbackStorage, MAX_PER_HOUR
from services.generator import (
    generate_many,
    generate_many_random,
    generate_similar,
    score_username,
)
from services.quota import QuotaStorage
from services.session_guard import SessionGuard
from services.storage import FoundStorage
from services.watchlist import Watchlist

logger = logging.getLogger(__name__)
router = Router(name="userneed")


# =============================================================================
#                              Константы
# =============================================================================

BULK_LIMIT = 200
GEN_BATCH = 10                 # минимальный размер пакета генерации
GEN_MAX_FACTOR = 25            # потолок проверок = target × фактор
MAX_EMPTY_BATCHES = 3          # подряд пустых пакетов → генератор исчерпан
PROGRESS_EDIT_INTERVAL = 1.5   # сек между правками прогресса (лимиты Telegram)
SIMILAR_MIN_BASE_LEN = 3
SIMILAR_MAX_BASE_LEN = 15
SIMILAR_QUICK_TARGET = 3
WATCH_LIMIT = 2000
WATCH_LIST_PREVIEW = 120
GEN_HARD_TIMEOUT = 900         # сек; ключевое решение №7 в PROJECT.md
GEN_SOFT_TIMEOUT = GEN_HARD_TIMEOUT - 60   # штатная остановка с частичным результатом
SEMAPHORE_WAIT = 0.2           # сколько ждём свободный слот, прежде чем отказать
FEEDBACK_MIN_LEN = 5
FEEDBACK_MAX_LEN = 1000
FEEDBACK_PREVIEW_LEN = 60


# =============================================================================
#                        Стейт-машины
# =============================================================================

class BulkStates(StatesGroup):
    waiting_list = State()


class WatchStates(StatesGroup):
    waiting_add = State()
    waiting_del = State()


class SimilarStates(StatesGroup):
    waiting_base = State()


class FeedbackStates(StatesGroup):
    waiting_text = State()


# =============================================================================
#                 Глобальный семафор и активные прогоны
# =============================================================================

_gen_semaphore: asyncio.Semaphore | None = None


def init_gen_semaphore(limit: int) -> None:
    """Вызывается из main.py после создания event loop."""
    global _gen_semaphore
    _gen_semaphore = asyncio.Semaphore(max(1, limit))


@dataclass
class _Run:
    """Один активный прогон (генерация или пакетная проверка) пользователя."""
    user_id: int
    kind: str
    cfg: Config
    quota: QuotaStorage
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)
    consumed: bool = False

    def commit(self) -> str:
        """Списывает сеанс (ровно один раз) и возвращает строку о лимите."""
        if not self.consumed:
            self.consumed = True
            if not self.cfg.is_admin(self.user_id):
                self.quota.consume(self.user_id)
                logger.info(
                    "Квота: user=%s списан сеанс (%s), осталось %d",
                    self.user_id, self.kind, self.quota.remaining(self.user_id),
                )
        return _usage_line(self.user_id, self.cfg, self.quota)

    def usage_line(self) -> str:
        return _usage_line(self.user_id, self.cfg, self.quota)

    @property
    def time_is_up(self) -> bool:
        """Мягкий лимит: прогон сворачивается сам и показывает, что успел."""
        return time.monotonic() - self.started_at >= GEN_SOFT_TIMEOUT


# user_id → активный прогон. Второй прогон того же пользователя не допускаем.
_active_runs: dict[int, _Run] = {}


# =============================================================================
#                          Квота
# =============================================================================

def _remaining(user_id: int, cfg: Config, quota: QuotaStorage) -> int:
    """Остаток сеансов. -1 — оператор (без ограничений)."""
    if cfg.is_admin(user_id):
        return -1
    return quota.remaining(user_id)


def _limit_for(user_id: int, cfg: Config, quota: QuotaStorage) -> int:
    """Персональный потолок сеансов (база + выданные оператором)."""
    if cfg.is_admin(user_id):
        return cfg.usage_limit
    return quota.allowed(user_id)


def _usage_line(user_id: int, cfg: Config, quota: QuotaStorage) -> str:
    return B.remaining_line(
        _remaining(user_id, cfg, quota), _limit_for(user_id, cfg, quota), cfg.is_admin(user_id)
    )


def _is_new_user(user_id: int, cfg: Config, quota: QuotaStorage) -> bool:
    return not cfg.is_admin(user_id) and quota.is_new(user_id)


# =============================================================================
#                             Middleware
# =============================================================================

class AccessMiddleware(BaseMiddleware):
    """
    Закрытая бета. Регистрируется как OUTER middleware на message и
    callback_query — срабатывает до фильтров и хендлеров, обойти нельзя.
    События без from_user (посты каналов, анонимные админы) в бете режем.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        cfg = self._cfg
        if not cfg.beta_mode:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user is not None and cfg.has_access(user.id):
            return await handler(event, data)

        logger.info(
            "Доступ запрещён: user=%s event=%s",
            user.id if user else None, type(event).__name__,
        )
        if isinstance(event, CallbackQuery):
            with contextlib.suppress(TelegramAPIError):
                await event.answer(B.NO_ACCESS_ALERT, show_alert=True)
        elif isinstance(event, Message) and user is not None:
            with contextlib.suppress(TelegramAPIError):
                await event.answer(B.no_access_text(user.id))
        return None


class LogAllMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        uid = user.id if user else "?"
        if isinstance(event, CallbackQuery):
            msg = event.message
            chat_id = msg.chat.id if isinstance(msg, Message) else "?"
            logger.info("cb=%r user=%s chat=%s", event.data, uid, chat_id)
        elif isinstance(event, Message):
            preview = (event.text or event.caption or "")[:60].replace("\n", " ")
            logger.info("msg=%r user=%s chat=%s", preview, uid, event.chat.id)
        return await handler(event, data)


# =============================================================================
#                          Вспомогательные
# =============================================================================

def _cb_message(call: CallbackQuery) -> Message | None:
    """Сообщение под кнопкой, если оно ещё доступно для правки."""
    msg = call.message
    return msg if isinstance(msg, Message) else None


async def _safe_edit(
    target: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    Правит текст или подпись. True — получилось (или менять было нечего).
    False — не вышло: сообщение удалено/устарело/лимит Telegram.
    """
    try:
        if target.photo:
            await target.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await target.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramRetryAfter as exc:
        logger.warning("edit: flood control, повтор через %s с", exc.retry_after)
        return False
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return True
        logger.debug("edit не удался: %s", exc)
        return False


async def _edit_or_caption(
    target: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Правит сообщение, а если нельзя — отправляет новое, чтобы ответ не пропал."""
    if await _safe_edit(target, text, reply_markup):
        return
    with contextlib.suppress(TelegramAPIError):
        await target.answer(text, reply_markup=reply_markup)


async def _show_welcome(message: Message, cfg: Config, quota: QuotaStorage) -> None:
    user = message.from_user
    assert user is not None
    is_admin = cfg.is_admin(user.id)
    text = B.welcome(
        remaining=_remaining(user.id, cfg, quota),
        limit=_limit_for(user.id, cfg, quota),
        is_admin=is_admin,
        first_name=user.first_name or "",
        show_watch=is_admin,
    )
    markup = main_menu(is_admin=is_admin)

    photo: str | FSInputFile | None = None
    if cfg.banner_path.lower().startswith(("http://", "https://")):
        photo = cfg.banner_path
    elif cfg.banner_path:
        p = Path(cfg.banner_path)
        if p.is_file():
            photo = FSInputFile(p)

    if photo is not None:
        try:
            await message.answer_photo(photo=photo, caption=text, reply_markup=markup)
            return
        except TelegramAPIError as exc:
            logger.warning("Баннер не отправился (%s) — шлю текст", exc)
    await message.answer(text, reply_markup=markup)


async def _admin_only(message: Message, cfg: Config) -> bool:
    """True — можно продолжать. Иначе отвечает отказом."""
    if message.from_user is not None and cfg.is_admin(message.from_user.id):
        return True
    await message.answer(B.ADMIN_ONLY)
    return False


def _parse_usernames(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.splitlines():
        for token in line.replace(",", " ").split():
            name = token.lstrip("@").strip().lower()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _is_similar_base(base: str) -> bool:
    return (
        base.isascii()
        and base.isalpha()
        and SIMILAR_MIN_BASE_LEN <= len(base) <= SIMILAR_MAX_BASE_LEN
    )


def _split_results(
    results: dict[str, CheckResult],
    order: Iterable[str],
) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "free": [], "fragment": [], "taken": [],
        "invalid": [], "error": [], "unchecked": [],
    }
    for name in order:
        buckets[results.get(name, "unchecked")].append(name)
    return buckets


def _when(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


async def _send_chunks(
    target: Message,
    lines: list[str],
    last_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Режет длинный отчёт на сообщения; клавиатура — только под последним."""
    chunks = chunk_lines(lines)
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        await target.answer(chunk, reply_markup=last_markup if is_last else None)


# =============================================================================
#                      Общая обвязка прогона
# =============================================================================

RunBody = Callable[[_Run], Awaitable[None]]


async def _start_run(
    *,
    kind: str,
    user_id: int,
    status: Message,
    cfg: Config,
    quota: QuotaStorage,
    body: RunBody,
) -> None:
    """
    Лимит → один прогон на пользователя → глобальный семафор → таймаут.

    body получает _Run и сам решает, когда вызвать run.commit() (списать
    сеанс). Если body не дошёл до commit — сеанс остаётся у пользователя.
    """
    if _remaining(user_id, cfg, quota) == 0:
        await _edit_or_caption(status, B.limit_reached(_limit_for(user_id, cfg, quota)),
                               reply_markup=back_to_main())
        return

    if user_id in _active_runs:
        await _edit_or_caption(status, B.RUN_ALREADY_ACTIVE,
                               reply_markup=back_to_main())
        return

    # Резервируем слот пользователя ДО ожидания семафора — иначе двойной тап
    # по кнопке успел бы пройти проверку выше дважды.
    run = _Run(user_id=user_id, kind=kind, cfg=cfg, quota=quota)
    _active_runs[user_id] = run
    try:
        sem = _gen_semaphore
        if sem is not None:
            # locked() + acquire() без await между ними — атомарно для event loop,
            # поэтому нет гонки «таймаут и захват одновременно», как у wait_for.
            if sem.locked():
                await asyncio.sleep(SEMAPHORE_WAIT)
            if sem.locked():
                await _edit_or_caption(status, B.queue_wait_text(cfg.gen_busy_retry_hint),
                                       reply_markup=back_to_main())
                return
            await sem.acquire()

        run.started_at = time.monotonic()
        logger.info("Прогон %s: user=%s старт", kind, user_id)
        try:
            await asyncio.wait_for(body(run), timeout=GEN_HARD_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(
                "Прогон %s user=%s превысил таймаут %d с.", kind, user_id, GEN_HARD_TIMEOUT,
            )
            await _edit_or_caption(status, B.error_generic(B.GEN_TIMEOUT_REASON),
                                   reply_markup=back_to_main())
        except AllSessionsFloodedError as exc:
            await _edit_or_caption(status, B.flooded_pause(exc.seconds_until_next),
                                   reply_markup=back_to_main())
        except TelegramAPIError as exc:
            logger.exception("Прогон %s user=%s: ошибка Telegram API: %s", kind, user_id, exc)
        finally:
            if sem is not None:
                sem.release()
            logger.info(
                "Прогон %s: user=%s финиш за %.0f с (сеанс %s)",
                kind, user_id, time.monotonic() - run.started_at,
                "списан" if run.consumed else "не списан",
            )
    finally:
        _active_runs.pop(user_id, None)


async def _check_many(
    names: Iterable[str],
    *,
    run: _Run,
    checker: TelegramChecker,
    concurrency: int,
    on_result: Callable[[str, CheckResult], Awaitable[None]],
    should_stop: Callable[[], bool] | None = None,
) -> int | None:
    """
    Проверяет имена пулом воркеров. Имена берутся по одному, поэтому
    остановка срабатывает сразу, а не после всей очереди.

    Возвращает None, если дошли до конца, или секунды до ближайшей
    свободной сессии, если упёрлись во флуд всех сессий.
    """
    queue = iter(list(names))
    flooded: list[int] = []

    def _halt() -> bool:
        return (
            bool(flooded)
            or run.stop.is_set()
            or run.time_is_up
            or (should_stop is not None and should_stop())
        )

    async def worker() -> None:
        for name in queue:
            if _halt():
                return
            try:
                res = await checker.check(name)
            except AllSessionsFloodedError as exc:
                flooded.append(exc.seconds_until_next)
                return
            await on_result(name, res)

    pool = max(1, concurrency)
    await asyncio.gather(*(worker() for _ in range(pool)))
    return flooded[0] if flooded else None


# =============================================================================
#                              Команды
# =============================================================================

@router.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, cfg: Config, quota: QuotaStorage,
) -> None:
    await state.clear()
    await _show_welcome(message, cfg, quota)


@router.message(Command("help"))
async def cmd_help(message: Message, cfg: Config) -> None:
    await message.answer(
        B.help_text(cfg.usage_limit, BULK_LIMIT, GEN_HARD_TIMEOUT // 60),
        reply_markup=back_to_main(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, cfg: Config) -> None:
    await state.clear()
    run = _active_runs.get(message.from_user.id) if message.from_user else None
    if run is not None:
        run.stop.set()
    is_admin = bool(message.from_user and cfg.is_admin(message.from_user.id))
    await message.answer(B.CANCELLED_TEXT, reply_markup=main_menu(is_admin=is_admin))


@router.message(Command("found"))
async def cmd_found(message: Message, storage: FoundStorage) -> None:
    assert message.from_user is not None
    await _show_found_list(message, storage, user_id=message.from_user.id, days=30)


# --- Операторские -----------------------------------------------------------

@router.message(Command("report_now"))
async def cmd_report_now(message: Message, reporter: DailyReporter, cfg: Config) -> None:
    if not await _admin_only(message, cfg):
        return
    await message.answer(B.REPORT_PREPARING)
    try:
        await reporter.send_now()
    except Exception as exc:  # noqa: BLE001 — отчёт не должен ронять хендлер
        logger.exception("report_now: %s", exc)
        await message.answer(B.report_failed(type(exc).__name__))


@router.message(Command("backup_now"))
async def cmd_backup_now(message: Message, backup_mgr: BackupManager, cfg: Config) -> None:
    if not await _admin_only(message, cfg):
        return
    status = await message.answer(B.BACKUP_PREPARING)
    try:
        info = await asyncio.to_thread(backup_mgr.make_backup)
    except Exception as exc:  # noqa: BLE001
        logger.exception("backup_now: %s", exc)
        await _edit_or_caption(status, B.backup_failed(type(exc).__name__))
        return
    await _edit_or_caption(
        status,
        B.backup_done(
            files=info["files"], size_mb=info["size_mb"], path=info["path"],
            removed=info["removed"], secrets_included=backup_mgr.include_secrets,
        ),
    )


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, feedback: FeedbackStorage, cfg: Config) -> None:
    """Последние обращения — только для админов."""
    if not await _admin_only(message, cfg):
        return
    records = feedback.recent(limit=20)
    if not records:
        await message.answer(B.FEEDBACK_EMPTY)
        return
    lines = B.feedback_list_header()
    for r in records:
        preview = r.text[:FEEDBACK_PREVIEW_LEN].replace("\n", " ")
        lines.append(B.feedback_list_line(
            r.ticket_id, r.user_id, _when(r.created_at), r.status, preview,
        ))
    await _send_chunks(message, lines)


@router.message(Command("today"))
async def cmd_today(
    message: Message, checker: TelegramChecker, storage: FoundStorage,
    guard: SessionGuard, cfg: Config,
) -> None:
    if not await _admin_only(message, cfg):
        return
    records, unique_users = storage.count_since(days=1)
    snap = guard.snapshot()
    alive = sum(1 for s in snap if s["alive"])
    await message.answer(B.today_stats(records, unique_users, checker.stats, alive, len(snap)))


def _session_rows(checker: TelegramChecker, guard: SessionGuard) -> list[dict[str, object]]:
    guard_map = {s["name"]: s for s in guard.snapshot()}
    rows: list[dict[str, object]] = []
    for s in checker.sessions_snapshot():
        row = dict(s)
        row["alive"] = guard_map.get(str(s["name"]), {}).get("alive", True)
        rows.append(row)
    return rows


@router.message(Command("sessions"))
async def cmd_sessions(
    message: Message, checker: TelegramChecker, guard: SessionGuard, cfg: Config,
) -> None:
    if not await _admin_only(message, cfg):
        return
    await message.answer(B.sessions_state(_session_rows(checker, guard)))


@router.message(Command("stats"))
async def cmd_stats(
    message: Message,
    checker: TelegramChecker,
    watchlist: Watchlist,
    storage: FoundStorage,
    guard: SessionGuard,
    feedback: FeedbackStorage,
    check_cache: CheckCache,
    cfg: Config,
) -> None:
    if not await _admin_only(message, cfg):
        return
    await message.answer(B.service_state(
        sessions=_session_rows(checker, guard),
        st=checker.stats,
        storage_stats=storage.stats(),
        watch=watchlist.breakdown(),
        cache=check_cache.stats(),
        feedback_count=feedback.count(),
        beta_mode=cfg.beta_mode,
        invited=len(cfg.allowed_user_ids),
        quota_limit=cfg.usage_limit,
    ))


def _parse_user_id(command: CommandObject) -> int | None:
    args = (command.args or "").split()
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


@router.message(Command("quota"))
async def cmd_quota(
    message: Message, command: CommandObject, quota: QuotaStorage, cfg: Config,
) -> None:
    if not await _admin_only(message, cfg):
        return
    target = _parse_user_id(command)
    if target is None:
        await message.answer(B.QUOTA_USAGE_HINT)
        return
    await message.answer(B.quota_info(
        target, quota.used(target), quota.remaining(target), quota.allowed(target),
    ))


@router.message(Command("quota_reset"))
async def cmd_quota_reset(
    message: Message, command: CommandObject, quota: QuotaStorage, cfg: Config,
) -> None:
    if not await _admin_only(message, cfg):
        return
    target = _parse_user_id(command)
    if target is None:
        await message.answer(B.QUOTA_USAGE_HINT)
        return
    assert message.from_user is not None
    quota.reset(target)
    logger.info("Квота: admin=%s сбросил user=%s", message.from_user.id, target)
    await message.answer(B.quota_reset_done(target, quota.allowed(target)))


@router.message(Command("quota_add"))
async def cmd_quota_add(
    message: Message, command: CommandObject, quota: QuotaStorage, cfg: Config,
) -> None:
    if not await _admin_only(message, cfg):
        return
    args = (command.args or "").split()
    try:
        target, extra = int(args[0]), int(args[1])
    except (IndexError, ValueError):
        await message.answer(B.QUOTA_USAGE_HINT)
        return
    assert message.from_user is not None
    quota.grant(target, extra)
    logger.info("Квота: admin=%s выдал user=%s +%d", message.from_user.id, target, extra)
    await message.answer(B.quota_info(
        target, quota.used(target), quota.remaining(target), quota.allowed(target),
    ))


# =============================================================================
#                        Навигация
# =============================================================================

@router.callback_query(F.data == "menu:main")
async def cb_main_menu(
    call: CallbackQuery, state: FSMContext, cfg: Config, quota: QuotaStorage,
) -> None:
    await state.clear()
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    is_admin = cfg.is_admin(call.from_user.id)
    text = B.welcome(
        remaining=_remaining(call.from_user.id, cfg, quota),
        limit=_limit_for(call.from_user.id, cfg, quota),
        is_admin=is_admin,
        first_name=call.from_user.first_name or "",
        show_watch=is_admin,
    )
    await _edit_or_caption(msg, text, reply_markup=main_menu(is_admin=is_admin))
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery, cfg: Config) -> None:
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await _edit_or_caption(
        msg, B.help_text(cfg.usage_limit, BULK_LIMIT, GEN_HARD_TIMEOUT // 60),
        reply_markup=back_to_main(),
    )
    await call.answer()


# =============================================================================
#                            Модуль 06 · Обратная связь
# =============================================================================

@router.callback_query(F.data == "feedback:start")
async def cb_feedback_start(
    call: CallbackQuery, state: FSMContext, feedback: FeedbackStorage,
) -> None:
    user_id = call.from_user.id
    if not feedback.rating_ok(user_id, MAX_PER_HOUR):
        wait_sec = feedback.seconds_until_next(user_id)
        await call.answer(
            B.feedback_rate_limited(max(1, wait_sec // 60), MAX_PER_HOUR),
            show_alert=True,
        )
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await state.set_state(FeedbackStates.waiting_text)
    await _edit_or_caption(
        msg, B.feedback_intro(MAX_PER_HOUR, FEEDBACK_MIN_LEN, FEEDBACK_MAX_LEN),
        reply_markup=cancel_menu(),
    )
    await call.answer()


@router.message(FeedbackStates.waiting_text, F.text)
async def handle_feedback_text(
    message: Message,
    state: FSMContext,
    feedback: FeedbackStorage,
    bot: Bot,
    cfg: Config,
) -> None:
    user = message.from_user
    assert user is not None
    body = (message.text or "").strip()

    # Неверная длина — остаёмся в состоянии, пользователь просто шлёт ещё раз.
    if not (FEEDBACK_MIN_LEN <= len(body) <= FEEDBACK_MAX_LEN):
        await message.answer(
            B.feedback_invalid_length(FEEDBACK_MIN_LEN, FEEDBACK_MAX_LEN),
            reply_markup=cancel_menu(),
        )
        return

    await state.clear()

    if not feedback.rating_ok(user.id, MAX_PER_HOUR):
        wait_sec = feedback.seconds_until_next(user.id)
        await message.answer(
            B.feedback_rate_limited(max(1, wait_sec // 60), MAX_PER_HOUR),
            reply_markup=back_to_main(),
        )
        return

    if not cfg.admin_ids:
        await message.answer(B.ADMINS_UNAVAILABLE, reply_markup=back_to_main())
        return

    ticket_id = 0
    sent_to = 0
    for admin_id in sorted(cfg.admin_ids):
        # Сначала запись — чтобы в шапке у админа был настоящий номер.
        record_id = feedback.save(
            user_id=user.id, admin_id=admin_id, text=body,
            ticket_id=ticket_id or None,
        )
        if ticket_id == 0:
            ticket_id = record_id
        text = B.admin_feedback_message(
            ticket_id, user.id, user.username or "", user.first_name or "", body,
        )
        try:
            sent = await bot.send_message(admin_id, text)
        except TelegramAPIError as exc:
            logger.warning("feedback: не доставлено админу %s: %s", admin_id, exc)
            feedback.mark_failed(record_id)
            continue
        feedback.set_admin_msg(record_id, sent.chat.id, sent.message_id)
        sent_to += 1

    if sent_to == 0:
        await message.answer(B.FEEDBACK_DELIVERY_FAILED, reply_markup=back_to_main())
        return

    await message.answer(B.feedback_thanks(ticket_id), reply_markup=back_to_main())
    logger.info("feedback: user=%s ticket=#%s sent_to=%d", user.id, ticket_id, sent_to)


@router.message(FeedbackStates.waiting_text)
async def feedback_wrong_input(message: Message) -> None:
    await message.answer(B.FEEDBACK_TEXT_EXPECTED, reply_markup=cancel_menu())


@router.message(F.reply_to_message)
async def handle_admin_reply(
    message: Message,
    feedback: FeedbackStorage,
    bot: Bot,
    cfg: Config,
) -> None:
    """
    Админ отвечает reply на сообщение-обращение — отправляем ответ
    пользователю. Если reply не на наше сообщение или отправитель не админ —
    SkipHandler, чтобы не мешать другим хендлерам.
    """
    if message.from_user is None or not cfg.is_admin(message.from_user.id):
        raise SkipHandler
    reply = message.reply_to_message
    if reply is None:
        raise SkipHandler
    record = feedback.find_by_admin_msg(
        admin_chat_id=message.chat.id, admin_msg_id=reply.message_id,
    )
    if record is None:
        raise SkipHandler

    answer = (message.text or "").strip()
    if not answer:
        await message.reply(B.ADMIN_REPLY_EMPTY)
        return

    try:
        await bot.send_message(record.user_id, B.admin_reply_to_user(record.ticket_id, answer))
    except TelegramAPIError as exc:
        logger.warning("feedback: ответ пользователю %s не доставлен: %s", record.user_id, exc)
        await message.reply(B.ADMIN_REPLY_FAILED)
        return

    feedback.mark_answered(record.ticket_id)
    await message.reply(B.admin_reply_delivered(record.user_id, record.ticket_id))


# =============================================================================
#                    Модуль 01 · Выбор режима
# =============================================================================

async def _gen_screen(
    call: CallbackQuery, cfg: Config, quota: QuotaStorage,
    text_for: Callable[[int], str], markup: InlineKeyboardMarkup,
) -> None:
    """Общий шаблон экранов генерации: лимит → текст → правка сообщения."""
    user_id = call.from_user.id
    rem = _remaining(user_id, cfg, quota)
    if rem == 0:
        await call.answer(B.limit_reached(_limit_for(user_id, cfg, quota)), show_alert=True)
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await _edit_or_caption(msg, text_for(rem), reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "gen:start")
async def cb_gen_start(
    call: CallbackQuery, cfg: Config, state: FSMContext, quota: QuotaStorage,
) -> None:
    await state.clear()
    user_id = call.from_user.id
    await _gen_screen(
        call, cfg, quota,
        lambda rem: B.gen_mode_intro(
            user_id, rem, _limit_for(user_id, cfg, quota), is_new=_is_new_user(user_id, cfg, quota),
        ),
        gen_mode_menu(),
    )


@router.callback_query(F.data == "gen:mode:smart")
async def cb_gen_smart(call: CallbackQuery, cfg: Config, quota: QuotaStorage) -> None:
    await _gen_screen(
        call, cfg, quota,
        lambda rem: B.gen_count_intro("smart", rem, _limit_for(call.from_user.id, cfg, quota)),
        gen_count_menu("smart", cfg.gen_max_smart),
    )


@router.callback_query(F.data == "gen:mode:random")
async def cb_gen_random(call: CallbackQuery, cfg: Config, quota: QuotaStorage) -> None:
    await _gen_screen(
        call, cfg, quota,
        lambda rem: B.gen_count_intro("random", rem, _limit_for(call.from_user.id, cfg, quota)),
        gen_count_menu("random", cfg.gen_max_random),
    )


@router.callback_query(F.data == "gen:mode:similar")
async def cb_gen_similar_start(
    call: CallbackQuery, cfg: Config, state: FSMContext, quota: QuotaStorage,
) -> None:
    user_id = call.from_user.id
    rem, limit = _remaining(user_id, cfg, quota), _limit_for(user_id, cfg, quota)
    if rem == 0:
        await call.answer(B.limit_reached(limit), show_alert=True)
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await state.set_state(SimilarStates.waiting_base)
    await _edit_or_caption(msg, B.gen_similar_intro(rem, limit), reply_markup=cancel_menu())
    await call.answer()


@router.message(SimilarStates.waiting_base, F.text)
async def handle_similar_base(
    message: Message, state: FSMContext, cfg: Config, quota: QuotaStorage,
) -> None:
    base = (message.text or "").lstrip("@").strip().lower()
    if not _is_similar_base(base):
        await message.answer(B.similar_base_invalid(SIMILAR_MAX_BASE_LEN),
                             reply_markup=cancel_menu())
        return

    candidates = generate_similar(base, count=cfg.similar_max_results)
    if not candidates:
        await message.answer(B.similar_no_candidates(base), reply_markup=cancel_menu())
        return

    await state.clear()
    assert message.from_user is not None
    user_id = message.from_user.id
    await message.answer(
        B.gen_similar_pick_count(
            base, len(candidates),
            _remaining(user_id, cfg, quota), _limit_for(user_id, cfg, quota),
        ),
        reply_markup=gen_similar_count_menu(base, cfg.gen_max_smart),
    )


@router.message(SimilarStates.waiting_base)
async def similar_wrong_input(message: Message) -> None:
    await message.answer(B.similar_base_invalid(SIMILAR_MAX_BASE_LEN),
                         reply_markup=cancel_menu())


@router.callback_query(F.data == "gen:stop")
async def cb_gen_stop(call: CallbackQuery) -> None:
    run = _active_runs.get(call.from_user.id)
    if run is None:
        await call.answer(B.GEN_NO_ACTIVE_RUN)
        return
    run.stop.set()
    await call.answer(B.GEN_STOPPING)


# =============================================================================
#                    Запуск генерации — роутеры
# =============================================================================

@router.callback_query(F.data.startswith("gen:run:"))
async def cb_gen_run(
    call: CallbackQuery, checker: TelegramChecker, storage: FoundStorage,
    cfg: Config, quota: QuotaStorage,
) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 4:
        await call.answer(B.CB_BAD_REQUEST, show_alert=True)
        return
    mode = parts[2]
    try:
        target = int(parts[3])
    except ValueError:
        await call.answer(B.CB_BAD_COUNT, show_alert=True)
        return
    if mode not in {"smart", "random"}:
        await call.answer(B.CB_BAD_MODE, show_alert=True)
        return
    max_allowed = cfg.gen_max_smart if mode == "smart" else cfg.gen_max_random
    if not 1 <= target <= max_allowed:
        await call.answer(B.count_out_of_range(max_allowed), show_alert=True)
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await call.answer()
    await _run_generation(
        status=msg, user_id=call.from_user.id, mode=mode, target=target, base=None,
        checker=checker, storage=storage, cfg=cfg, quota=quota,
    )


@router.callback_query(F.data.startswith("gen:sim:"))
async def cb_gen_similar_run(
    call: CallbackQuery, checker: TelegramChecker, storage: FoundStorage,
    cfg: Config, quota: QuotaStorage,
) -> None:
    parts = (call.data or "").split(":")
    if len(parts) != 4:
        await call.answer(B.CB_BAD_REQUEST, show_alert=True)
        return
    base = parts[2]
    try:
        target = int(parts[3])
    except ValueError:
        await call.answer(B.CB_BAD_COUNT, show_alert=True)
        return
    if not _is_similar_base(base):
        await call.answer(B.CB_BAD_BASE, show_alert=True)
        return
    if not 1 <= target <= cfg.gen_max_smart:
        await call.answer(B.count_out_of_range(cfg.gen_max_smart), show_alert=True)
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await call.answer()
    await _run_generation(
        status=msg, user_id=call.from_user.id, mode="similar", target=target, base=base,
        checker=checker, storage=storage, cfg=cfg, quota=quota,
    )


# =============================================================================
#                    Кнопки под отчётом
# =============================================================================

@router.callback_query(F.data.startswith("ack_claim:"))
async def cb_ack_claim(
    call: CallbackQuery, storage: FoundStorage, watchlist: Watchlist, cfg: Config,
) -> None:
    username = (call.data or "").split(":", 1)[1].strip().lower()
    if not is_valid_username_format(username):
        await call.answer(B.CB_BAD_REQUEST, show_alert=True)
        return
    user_id = call.from_user.id
    is_admin = cfg.is_admin(user_id)

    # Пользователь помечает только СВОИ находки; оператор — любые.
    changed = storage.mark_claimed(username, user_id=None if is_admin else user_id)
    if changed == 0 and not is_admin:
        await call.answer(B.CLAIM_NOT_YOURS, show_alert=True)
        return
    if is_admin:
        watchlist.remove(username)

    logger.info("ack_claim:%s — user=%s", username, user_id)
    await call.answer(B.claim_ack_alert(username))
    msg = _cb_message(call)
    if msg is not None:
        await msg.answer(B.claim_ack_text(username), reply_markup=back_to_main())


@router.callback_query(F.data.startswith("sim_base:"))
async def cb_similar_from(
    call: CallbackQuery, checker: TelegramChecker, storage: FoundStorage,
    cfg: Config, quota: QuotaStorage,
) -> None:
    base = (call.data or "").split(":", 1)[1].strip().lower()
    if not _is_similar_base(base):
        await call.answer(B.CB_BAD_BASE, show_alert=True)
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    logger.info("sim_base:%s — user=%s", base, call.from_user.id)
    await call.answer(B.SIMILAR_PREPARING)
    status = await msg.answer(B.similar_quick_header(base))
    await _run_generation(
        status=status, user_id=call.from_user.id, mode="similar",
        target=SIMILAR_QUICK_TARGET, base=base,
        checker=checker, storage=storage, cfg=cfg, quota=quota,
    )


# =============================================================================
#                          Тело генерации
# =============================================================================

async def _run_generation(
    *,
    status: Message,
    user_id: int,
    mode: str,
    target: int,
    base: str | None,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
    quota: QuotaStorage,
) -> None:
    async def body(run: _Run) -> None:
        await _generation_body(
            run=run, status=status, mode=mode, target=target, base=base,
            checker=checker, storage=storage, cfg=cfg,
        )

    await _start_run(kind=mode, user_id=user_id, status=status,
                     cfg=cfg, quota=quota, body=body)


async def _generation_body(
    *,
    run: _Run,
    status: Message,
    mode: str,
    target: int,
    base: str | None,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    found: list[tuple[str, int, str]] = []
    tested = 0
    attempts_cap = target * GEN_MAX_FACTOR
    seen: set[str] = set(storage.claimed_names())   # «✓ Занял» не предлагаем
    last_edit = 0.0
    progress_lock = asyncio.Lock()

    def _next_batch(n: int) -> list[str]:
        if mode == "smart":
            return generate_many(n, exclude=seen)
        if mode == "random":
            return generate_many_random(n, exclude=seen)
        return generate_similar(base or "", count=n, exclude=seen)

    async def _tick(force: bool = False) -> None:
        nonlocal last_edit
        if not force and (
            progress_lock.locked() or time.monotonic() - last_edit < PROGRESS_EDIT_INTERVAL
        ):
            return
        async with progress_lock:
            now = time.monotonic()
            if not force and now - last_edit < PROGRESS_EDIT_INTERVAL:
                return
            last_edit = now
            await _safe_edit(status, B.search_progress(target, found, tested),
                             reply_markup=gen_progress_kb(len(found)))

    async def _on_result(name: str, res: CheckResult) -> None:
        nonlocal tested
        tested += 1
        if res != "free":
            await _tick()
            return
        sc, why = score_username(name)
        if sc < cfg.filter_min_score:
            await _tick()
            return
        found.append((name, sc, why))
        await _tick(force=True)

    await _edit_or_caption(status, B.search_started(target, base=base),
                           reply_markup=gen_progress_kb(0))

    flooded_for: int | None = None
    empty_batches = 0
    while (
        len(found) < target
        and tested < attempts_cap
        and not run.stop.is_set()
        and not run.time_is_up
    ):
        want = min(max(GEN_BATCH, (target - len(found)) * 2), attempts_cap - tested)
        batch = [n for n in _next_batch(want) if n not in seen]
        if not batch:
            empty_batches += 1
            if mode == "similar" or empty_batches >= MAX_EMPTY_BATCHES:
                logger.info("Генерация %s: кандидаты исчерпаны (проверено %d)", mode, tested)
                break
            await asyncio.sleep(0)   # отдаём управление циклу, не крутимся вхолостую
            continue
        empty_batches = 0
        seen.update(batch)

        flooded_for = await _check_many(
            batch, run=run, checker=checker, concurrency=cfg.check_concurrency,
            on_result=_on_result, should_stop=lambda: len(found) >= target,
        )
        if flooded_for is not None:
            break

    stopped_early = run.stop.is_set() or flooded_for is not None or run.time_is_up

    # --- Ничего не нашли ------------------------------------------------------
    if not found:
        if flooded_for is not None:
            await _edit_or_caption(status, B.flooded_pause(flooded_for),
                                   reply_markup=back_to_main())
            return
        rline = run.commit() if tested > 0 else run.usage_line()
        await _edit_or_caption(status, B.gen_nothing_found(cfg.filter_min_score, tested, rline),
                               reply_markup=back_to_main())
        return

    # --- Есть результат ---------------------------------------------------------
    for name, sc, _ in found:
        storage.record(name, sc, mode, "free", run.user_id)

    if flooded_for is not None:
        rline, note = run.usage_line(), B.flood_note(flooded_for)
    else:
        rline = run.commit()
        note = B.time_limit_note(GEN_SOFT_TIMEOUT // 60) if run.time_is_up else ""

    await _send_chunks(
        status, B.search_final(found, tested, stopped_early, rline, note=note),
        last_markup=found_actions_kb(found),
    )
    await _safe_edit(status, B.gen_done_short(len(found), tested, stopped_early))


# =============================================================================
#                    Модуль 02 · Пакетная проверка
# =============================================================================

@router.callback_query(F.data == "bulk:start")
async def cb_bulk_start(
    call: CallbackQuery, state: FSMContext, cfg: Config, quota: QuotaStorage,
) -> None:
    user_id = call.from_user.id
    rem, limit = _remaining(user_id, cfg, quota), _limit_for(user_id, cfg, quota)
    if rem == 0:
        await call.answer(B.limit_reached(limit), show_alert=True)
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await state.set_state(BulkStates.waiting_list)
    await _edit_or_caption(msg, B.bulk_intro(rem, limit, BULK_LIMIT), reply_markup=cancel_menu())
    await call.answer()


@router.message(BulkStates.waiting_list, F.text)
async def handle_bulk(
    message: Message,
    state: FSMContext,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
    quota: QuotaStorage,
) -> None:
    names = _parse_usernames(message.text or "")
    if not names:
        await message.answer(B.BULK_EMPTY, reply_markup=cancel_menu())
        return
    if len(names) > BULK_LIMIT:
        await message.answer(B.bulk_too_many(len(names), BULK_LIMIT),
                             reply_markup=cancel_menu())
        return

    await state.clear()
    user_id = message.from_user.id  # type: ignore[union-attr]
    total = len(names)
    status = await message.answer(
        B.bulk_progress(0, total, 0, 0, progress_bar(0, total)),
        reply_markup=bulk_progress_kb(),
    )

    async def body(run: _Run) -> None:
        await _bulk_body(run=run, status=status, names=names,
                         checker=checker, storage=storage, cfg=cfg)

    await _start_run(kind="bulk", user_id=user_id, status=status,
                     cfg=cfg, quota=quota, body=body)


async def _bulk_body(
    *,
    run: _Run,
    status: Message,
    names: list[str],
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    total = len(names)
    results: dict[str, CheckResult] = {}
    free_count = frag_count = 0
    last_edit = 0.0
    progress_lock = asyncio.Lock()

    async def _on_result(name: str, res: CheckResult) -> None:
        nonlocal free_count, frag_count, last_edit
        results[name] = res
        if res == "free":
            free_count += 1
        elif res == "fragment":
            frag_count += 1
        done = len(results)
        now = time.monotonic()
        if progress_lock.locked() or (now - last_edit < PROGRESS_EDIT_INTERVAL and done != total):
            return
        async with progress_lock:
            last_edit = time.monotonic()
            await _safe_edit(
                status,
                B.bulk_progress(done, total, free_count, frag_count, progress_bar(done, total)),
                reply_markup=bulk_progress_kb(),
            )

    flooded_for = await _check_many(
        names, run=run, checker=checker, concurrency=cfg.check_concurrency,
        on_result=_on_result,
    )

    buckets = _split_results(results, names)
    if flooded_for is not None and not results:
        await _edit_or_caption(status, B.flooded_pause(flooded_for),
                               reply_markup=back_to_main())
        return

    for name in buckets["free"]:
        sc, _ = score_username(name)
        storage.record(name, sc, "bulk", "free", run.user_id)

    if flooded_for is not None:
        rline, note = run.usage_line(), B.flood_note(flooded_for)
    else:
        rline = run.commit()
        note = B.time_limit_note(GEN_SOFT_TIMEOUT // 60) if run.time_is_up else ""

    await _send_chunks(
        status,
        B.bulk_report(
            buckets["free"], buckets["fragment"], buckets["taken"],
            buckets["invalid"], buckets["error"], rline,
            unchecked=buckets["unchecked"], note=note,
        ),
        last_markup=back_to_main(),
    )
    await _safe_edit(
        status,
        B.bulk_done_short(len(buckets["free"]), total, stopped=bool(buckets["unchecked"])),
    )


@router.message(BulkStates.waiting_list)
async def bulk_wrong_input(message: Message) -> None:
    await message.answer(B.BULK_TEXT_EXPECTED, reply_markup=cancel_menu())


# =============================================================================
#                    Модуль 03 · Watchlist (только операторы)
# =============================================================================

async def _watch_guard(call: CallbackQuery, watchlist: Watchlist) -> Message | None:
    """Проверка прав + доступности сообщения. None — отвечать дальше не нужно."""
    if not watchlist.is_admin(call.from_user.id):
        await call.answer(B.ADMIN_ONLY_ALERT, show_alert=True)
        return None
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
    return msg


@router.callback_query(F.data == "watch:start")
async def cb_watch_start(call: CallbackQuery, watchlist: Watchlist) -> None:
    msg = await _watch_guard(call, watchlist)
    if msg is None:
        return
    await _edit_or_caption(msg, B.watch_intro(watchlist.count(), watchlist.breakdown()),
                           reply_markup=watch_menu())
    await call.answer()


@router.callback_query(F.data == "watch:list")
async def cb_watch_list(call: CallbackQuery, watchlist: Watchlist) -> None:
    msg = await _watch_guard(call, watchlist)
    if msg is None:
        return
    targets = watchlist.all_targets()
    if not targets:
        await _edit_or_caption(msg, B.WATCH_EMPTY, reply_markup=watch_menu())
        await call.answer()
        return

    lines = [B.watch_list_header(len(targets)), ""]
    for name, st in targets[:WATCH_LIST_PREVIEW]:
        lines.append(B.watch_list_line(B.STATUS_MARKS.get(st, "?"), name, st))
    if len(targets) > WATCH_LIST_PREVIEW:
        lines.append(B.watch_list_more(len(targets) - WATCH_LIST_PREVIEW))
    await _send_chunks(msg, lines, last_markup=watch_menu())
    await call.answer()


@router.callback_query(F.data == "watch:add")
async def cb_watch_add(call: CallbackQuery, state: FSMContext, watchlist: Watchlist) -> None:
    msg = await _watch_guard(call, watchlist)
    if msg is None:
        return
    await state.set_state(WatchStates.waiting_add)
    await _edit_or_caption(msg, B.WATCH_ADD_PROMPT, reply_markup=cancel_menu())
    await call.answer()


@router.message(WatchStates.waiting_add, F.text)
async def handle_watch_add(message: Message, state: FSMContext, watchlist: Watchlist) -> None:
    if not watchlist.is_admin(message.from_user.id):  # type: ignore[union-attr]
        await state.clear()
        await message.answer(B.ADMIN_ONLY)
        return
    await state.clear()
    names = _parse_usernames(message.text or "")
    if not names:
        await message.answer(B.BULK_EMPTY, reply_markup=watch_menu())
        return
    added = 0
    for n in names:
        if not is_valid_username_format(n):
            continue
        if watchlist.count() >= WATCH_LIMIT:
            break
        if watchlist.add(n):
            added += 1
    await message.answer(B.watch_added(added, len(names), watchlist.count()),
                         reply_markup=watch_menu())


@router.callback_query(F.data == "watch:del")
async def cb_watch_del(call: CallbackQuery, state: FSMContext, watchlist: Watchlist) -> None:
    msg = await _watch_guard(call, watchlist)
    if msg is None:
        return
    await state.set_state(WatchStates.waiting_del)
    await _edit_or_caption(msg, B.WATCH_DEL_PROMPT, reply_markup=cancel_menu())
    await call.answer()


@router.message(WatchStates.waiting_del, F.text)
async def handle_watch_del(message: Message, state: FSMContext, watchlist: Watchlist) -> None:
    if not watchlist.is_admin(message.from_user.id):  # type: ignore[union-attr]
        await state.clear()
        await message.answer(B.ADMIN_ONLY)
        return
    await state.clear()
    names = _parse_usernames(message.text or "")
    removed = sum(1 for n in names if watchlist.remove(n))
    await message.answer(B.watch_removed(removed, len(names), watchlist.count()),
                         reply_markup=watch_menu())


# =============================================================================
#                    Модуль 04 · История находок
# =============================================================================

@router.callback_query(F.data == "found:show")
async def cb_found_show(call: CallbackQuery, storage: FoundStorage) -> None:
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await _edit_or_caption(msg, B.found_intro(storage.stats()), reply_markup=found_menu())
    await call.answer()


_FOUND_FILTERS: dict[str, tuple[int, str, int]] = {
    "30": (30, "последние 30", 30),
    "7d": (7, "за 7 дней", 100),
    "30d": (30, "за 30 дней", 100),
}


@router.callback_query(F.data.startswith("found:list:"))
async def cb_found_list(call: CallbackQuery, storage: FoundStorage) -> None:
    key = (call.data or "").split(":", 2)[2]
    spec = _FOUND_FILTERS.get(key)
    if spec is None:
        await call.answer(B.CB_UNKNOWN_FILTER, show_alert=True)
        return
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    days, label, limit = spec
    await _show_found_list(
        msg, storage, user_id=call.from_user.id,
        days=days, limit=limit, label=label, as_new_message=False,
    )
    await call.answer()


async def _show_found_list(
    target: Message,
    storage: FoundStorage,
    *,
    user_id: int,
    days: int,
    limit: int = 30,
    label: str | None = None,
    as_new_message: bool = True,
) -> None:
    records = storage.recent(user_id=user_id, days=days, limit=limit)
    if not records:
        if as_new_message:
            await target.answer(B.FOUND_EMPTY_PERIOD, reply_markup=back_to_main())
        else:
            await _edit_or_caption(target, B.FOUND_EMPTY_PERIOD, reply_markup=found_menu())
        return

    lines = [B.found_list_header(len(records), label or f"последние {limit}"), ""]
    for i, r in enumerate(records, 1):
        lines.append(B.found_list_line(i, r.username, r.score, _when(r.found_at), r.claimed))
    await _send_chunks(target, lines, last_markup=found_menu())


@router.callback_query(F.data == "found:export")
async def cb_found_export(call: CallbackQuery, storage: FoundStorage) -> None:
    msg = _cb_message(call)
    if msg is None:
        await call.answer(B.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    user_id = call.from_user.id
    await call.answer(B.EXPORT_PREPARING)

    export_path = Path(tempfile.gettempdir()) / f"userneed_found_{user_id}.csv"
    n = storage.export_csv(export_path, user_id=user_id, days=None)
    if n == 0:
        await msg.answer(B.EXPORT_EMPTY, reply_markup=found_menu())
        return
    try:
        await msg.answer_document(
            FSInputFile(export_path, filename=f"userneed_found_{user_id}.csv"),
            caption=B.export_caption(n),
            reply_markup=found_menu(),
        )
    except TelegramAPIError as exc:
        logger.exception("Экспорт: не удалось отправить файл: %s", exc)
        await msg.answer(B.EXPORT_FAILED, reply_markup=found_menu())
    finally:
        with contextlib.suppress(OSError):
            export_path.unlink(missing_ok=True)


# =============================================================================
#                              Fallback
# =============================================================================

@router.message()
async def fallback(message: Message, cfg: Config) -> None:
    is_admin = bool(message.from_user and cfg.is_admin(message.from_user.id))
    await message.answer(B.FALLBACK_TEXT, reply_markup=main_menu(is_admin=is_admin))
