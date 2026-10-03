"""
Хендлеры aiogram 3.x для системы UserNeed.

Модули:
  01 — Генерация (smart / random / similar)
  02 — Пакетная проверка
  03 — Мониторинг (watchlist)
  04 — История находок (SQLite)
  05 — Регламент
  06 — Связь с админом (обратная связь)

Особенности:
  • Закрытая бета: AccessMiddleware пропускает только whitelist.
  • Глобальный семафор генерации — не больше N одновременных прогонов.
  • Жёсткий таймаут генерации — семафор не может застрять.
  • Кнопки «✓ Занял» и «↻ Похожие» под отчётом генерации.
  • Обратная связь: пользователь пишет — админ отвечает reply.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Iterable

from aiogram import BaseMiddleware, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot import branding as B
from bot.keyboards import (
    back_to_main,
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
from bot.utils import chunk_lines, esc, progress_bar
from config import Config
from services.backup_manager import BackupManager
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
from services.session_guard import SessionGuard
from services.storage import FoundStorage
from services.watchlist import Watchlist

logger = logging.getLogger(__name__)
router = Router()


# =============================================================================
#                              Константы
# =============================================================================

BULK_LIMIT = 200
GEN_BATCH = 10
GEN_MAX_FACTOR = 25
PROGRESS_EDIT_INTERVAL = 1.2
USAGE_LIMIT_FALLBACK = 3
SIMILAR_MAX_BASE_LEN = 15
SIMILAR_QUICK_TARGET = 3
WATCH_LIMIT = 2000
GEN_HARD_TIMEOUT = 900
FEEDBACK_MIN_LEN = 5
FEEDBACK_MAX_LEN = 1000


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
#                        Глобальный семафор генерации
# =============================================================================

_gen_semaphore: asyncio.Semaphore | None = None


def init_gen_semaphore(limit: int) -> None:
    global _gen_semaphore
    _gen_semaphore = asyncio.Semaphore(max(1, limit))


# =============================================================================
#                          Квота и флаги
# =============================================================================

user_usages: dict[int, int] = {}
stop_flags: dict[int, bool] = {}


def _is_admin(user_id: int, cfg: Config) -> bool:
    return user_id in cfg.admin_ids


def _limit(cfg: Config) -> int:
    return cfg.usage_limit or USAGE_LIMIT_FALLBACK


def _remaining(user_id: int, cfg: Config) -> int:
    if _is_admin(user_id, cfg):
        return -1
    return max(0, _limit(cfg) - user_usages.get(user_id, 0))


def _try_consume(user_id: int, cfg: Config) -> tuple[bool, int]:
    if _is_admin(user_id, cfg):
        return True, -1
    used = user_usages.get(user_id, 0)
    if used >= _limit(cfg):
        return False, 0
    user_usages[user_id] = used + 1
    return True, _limit(cfg) - (used + 1)


def _usage_line(user_id: int, cfg: Config) -> str:
    return B.remaining_line(
        _remaining(user_id, cfg), _limit(cfg), _is_admin(user_id, cfg)
    )


def _is_new_user(user_id: int, cfg: Config) -> bool:
    if _is_admin(user_id, cfg):
        return False
    return user_usages.get(user_id, 0) == 0


# =============================================================================
#                             Middleware
# =============================================================================

class AccessMiddleware(BaseMiddleware):
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    async def __call__(self, handler, event, data):
        cfg = self._cfg
        if not cfg.beta_mode:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        if user.id in cfg.admin_ids or user.id in cfg.allowed_user_ids:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer(
                "Доступ закрыт. Система в закрытой бете.", show_alert=True
            )
        elif isinstance(event, Message):
            await event.answer(B.NO_ACCESS_TEXT)
        return None


class LogAllMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            chat_id = event.message.chat.id if event.message else "?"
            logger.info("cb=%r user=%s chat=%s",
                        event.data, event.from_user.id, chat_id)
        elif isinstance(event, Message):
            preview = (event.text or "")[:60].replace("\n", " ")
            logger.info("msg=%r user=%s chat=%s",
                        preview, event.from_user.id, event.chat.id)
        return await handler(event, data)


# =============================================================================
#                          Вспомогательные
# =============================================================================

async def _show_welcome(message: Message, cfg: Config) -> None:
    text = B.welcome(
        remaining=_remaining(message.from_user.id, cfg),
        limit=_limit(cfg),
        is_admin=_is_admin(message.from_user.id, cfg),
        first_name=message.from_user.first_name or "",
    )
    if not cfg.banner_path:
        await message.answer(text, reply_markup=main_menu())
        return
    if cfg.banner_path.lower().startswith(("http://", "https://")):
        photo: str | FSInputFile = cfg.banner_path
    else:
        p = Path(cfg.banner_path)
        if not p.is_file():
            await message.answer(text, reply_markup=main_menu())
            return
        photo = FSInputFile(p)
    try:
        await message.answer_photo(photo=photo, caption=text,
                                   reply_markup=main_menu())
    except Exception:
        await message.answer(text, reply_markup=main_menu())


async def _edit_or_caption(target: Message, text: str, reply_markup=None) -> None:
    try:
        if target.photo:
            await target.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await target.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


def _parse_usernames(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.splitlines():
        for token in line.replace(",", " ").split():
            name = token.lstrip("@").strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


def _progress_text(done: int, total: int, free: int, fragment: int) -> str:
    pct = int(done * 100 / total) if total else 0
    bar = progress_bar(done, total)
    return (
        f"<b>МОДУЛЬ 02 · ОБРАБОТКА</b>\n"
        f"Обработано: <b>{done}</b> / {total}\n"
        f"<code>{bar}</code>  {pct}%\n"
        f"Свободно: <b>{free}</b>  ·  только за TON: <b>{fragment}</b>"
    )


def _split_results(
    results: dict[str, CheckResult],
    order: Iterable[str],
) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "free": [], "fragment": [], "taken": [],
        "invalid": [], "error": [],
    }
    for name in order:
        buckets[results.get(name, "error")].append(name)
    return buckets


def _record_found(
    storage: FoundStorage,
    found: list[tuple[str, int, str]],
    source: str,
    user_id: int,
) -> None:
    for name, sc, _ in found:
        storage.record(name, sc, source, "free", user_id)


# =============================================================================
#                              Команды
# =============================================================================

@router.message(CommandStart())
async def cmd_start(message: Message, cfg: Config) -> None:
    await _show_welcome(message, cfg)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(B.HELP_TEXT, reply_markup=back_to_main())


@router.message(Command("found"))
async def cmd_found(
    message: Message,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    await _show_found_list(
        message, storage, user_id=message.from_user.id, days=30
    )


@router.message(Command("report_now"))
async def cmd_report_now(
    message: Message,
    reporter: DailyReporter,
    cfg: Config,
) -> None:
    if message.from_user.id not in cfg.admin_ids:
        await message.answer("Операция доступна только операторам системы.")
        return
    await message.answer("Формирую сводку…")
    try:
        await reporter.send_now()
    except Exception as exc:
        logger.exception("report_now: %s", exc)
        await message.answer(
            f"Не удалось отправить сводку: <code>{esc(type(exc).__name__)}</code>"
        )


@router.message(Command("backup_now"))
async def cmd_backup_now(
    message: Message,
    backup_mgr: BackupManager,
    cfg: Config,
) -> None:
    if message.from_user.id not in cfg.admin_ids:
        await message.answer("Операция доступна только операторам системы.")
        return
    status = await message.answer("Создаю бэкап, это может занять до минуты…")
    try:
        info = await asyncio.to_thread(backup_mgr.make_backup)
        await status.edit_text(
            f"<b>BACKUP · ВЫПОЛНЕН</b>\n"
            f"{B._DOT}\n"
            f"файлов в архиве: <b>{info['files']}</b>\n"
            f"размер: <b>{info['size_mb']} МБ</b>\n"
            f"путь: <code>{esc(info['path'])}</code>"
            + (f"\n\n<i>удалено старых: {info['removed']}</i>"
               if info["removed"] else "")
        )
    except Exception as exc:
        logger.exception("backup_now: %s", exc)
        await status.edit_text(
            f"Не удалось создать бэкап: <code>{esc(type(exc).__name__)}</code>"
        )


@router.message(Command("feedback"))
async def cmd_feedback(
    message: Message,
    feedback: FeedbackStorage,
    cfg: Config,
) -> None:
    """Последние обращения — только для админов."""
    if message.from_user.id not in cfg.admin_ids:
        await message.answer("Операция доступна только операторам системы.")
        return

    records = feedback.recent(limit=20)
    if not records:
        await message.answer("Обращений пока нет.")
        return

    lines = ["<b>ПОСЛЕДНИЕ ОБРАЩЕНИЯ</b>", B._DOT, ""]
    for r in records:
        when = time.strftime("%d.%m %H:%M", time.localtime(r.created_at))
        status_mark = {
            "answered": "✓",
            "sent": "•",
            "failed": "✗",
        }.get(r.status, "?")
        preview = r.text[:60].replace("\n", " ")
        lines.append(
            f"  {status_mark}  <b>#{r.id}</b>  "
            f"<code>{r.user_id}</code>  ·  {when}\n"
            f"       <i>{esc(preview)}…</i>"
        )

    for chunk in chunk_lines(lines):
        await message.answer(chunk, disable_web_page_preview=True)


@router.message(Command("today"))
async def cmd_today(
    message: Message,
    checker: TelegramChecker,
    storage: FoundStorage,
    guard: SessionGuard,
    cfg: Config,
) -> None:
    if message.from_user.id not in cfg.admin_ids:
        await message.answer("Операция доступна только операторам системы.")
        return

    records = storage.recent(user_id=None, days=1, limit=100000)
    unique_users = len({r.found_by for r in records if r.found_by})

    snap = guard.snapshot()
    alive_count = sum(1 for s in snap if s["alive"])
    st = checker.stats

    text = (
        f"<b>СТАТИСТИКА ЗА 24 ЧАСА</b>\n"
        f"{B._DOT}\n\n"
        f"Записей в истории: <b>{len(records)}</b>\n"
        f"Уникальных пользователей: <b>{unique_users}</b>\n\n"
        f"<b>Сейчас в системе:</b>\n"
        f"    Свободных найдено: <b>{st.get('free', 0)}</b>\n"
        f"    Занятых отсеяно:  <b>{st.get('taken', 0)}</b>\n"
        f"    HTTP-отсев:       <b>{st.get('http_short_circuit', 0)}</b>\n"
        f"    Флудов получено:  <b>{st.get('floods', 0)}</b>\n"
        f"    Сессий живых:     <b>{alive_count} / {len(snap)}</b>"
    )
    await message.answer(text)


@router.message(Command("sessions"))
async def cmd_sessions(
    message: Message,
    checker: TelegramChecker,
    guard: SessionGuard,
    cfg: Config,
) -> None:
    if message.from_user.id not in cfg.admin_ids:
        await message.answer("Операция доступна только операторам системы.")
        return

    snap = checker.sessions_snapshot()
    guard_map = {s["name"]: s for s in guard.snapshot()}

    lines = [f"<b>СОСТОЯНИЕ СЕССИЙ</b>", B._DOT, ""]
    for s in snap:
        name = str(s["name"])
        alive = guard_map.get(name, {}).get("alive", True)
        alive_mark = "✓" if alive else "✗ недоступна"
        paused = int(s["paused_for"])
        pause_note = f", пауза {paused} с" if paused > 0 else ""
        lines.append(
            f"  {alive_mark}  <code>{esc(name)}</code>\n"
            f"       запросов: <b>{s['calls_made']}</b>, "
            f"флудов: <b>{s['times_flooded']}</b>{pause_note}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(
    message: Message,
    checker: TelegramChecker,
    watchlist: Watchlist,
    storage: FoundStorage,
    guard: SessionGuard,
    feedback: FeedbackStorage,
    cfg: Config,
) -> None:
    if message.from_user.id not in cfg.admin_ids:
        await message.answer("Операция доступна только операторам системы.")
        return

    snap = checker.sessions_snapshot()
    st = checker.stats
    stg = storage.stats()
    wb = watchlist.breakdown()
    gsnap = guard.snapshot()
    alive_count = sum(1 for s in gsnap if s["alive"])
    fb_count = len(feedback.recent(limit=1000))

    lines = ["<b>СОСТОЯНИЕ СЕРВИСА</b>", B._DOT, ""]
    lines.append(f"<b>Сессии:</b> <b>{alive_count}</b> из <b>{len(gsnap)}</b> живы")
    for s in snap:
        name = str(s["name"])
        alive = next(
            (g["alive"] for g in gsnap if g["name"] == name), True
        )
        mark = "✓" if alive else "✗"
        paused = int(s["paused_for"])
        note = f"  ·  пауза {paused} с" if paused > 0 else ""
        lines.append(
            f"  {mark}  <code>{esc(name)}</code> — "
            f"запросов: <b>{s['calls_made']}</b>, "
            f"флудов: {s['times_flooded']}{note}"
        )
    lines.append("")
    lines.append("<b>Проверки:</b>")
    lines.append(f"    HTTP-отсев:  <b>{st.get('http_short_circuit', 0)}</b>")
    lines.append(f"    Свободно:    <b>{st.get('free', 0)}</b>")
    lines.append(f"    За TON:      <b>{st.get('fragment', 0)}</b>")
    lines.append(f"    Занято:      <b>{st.get('taken', 0)}</b>")
    lines.append("")
    lines.append("<b>Хранилище:</b>")
    lines.append(f"    Всего:  <b>{stg['total']}</b>,  "
                 f"свободных: <b>{stg['free']}</b>")
    lines.append("")
    lines.append("<b>Watchlist:</b>")
    lines.append(f"    Всего: <b>{wb['total']}</b>  "
                 f"(hot {wb['hot']}, warm {wb['warm']}, "
                 f"cold {wb['cold']}, sleep {wb['sleeping']})")
    lines.append("")
    lines.append(f"<b>Обращения:</b> {fb_count}")
    lines.append("")
    lines.append("<b>Закрытая бета:</b>")
    lines.append(f"    режим: {'да' if cfg.beta_mode else 'нет'}  ·  "
                 f"приглашено: <b>{len(cfg.allowed_user_ids)}</b>")

    await message.answer("\n".join(lines))


# =============================================================================
#                        Навигация
# =============================================================================

@router.callback_query(F.data == "menu:main")
async def cb_main_menu(call: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    await state.clear()
    text = B.welcome(
        remaining=_remaining(call.from_user.id, cfg),
        limit=_limit(cfg),
        is_admin=_is_admin(call.from_user.id, cfg),
        first_name=call.from_user.first_name or "",
    )
    await _edit_or_caption(call.message, text, reply_markup=main_menu())
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery) -> None:
    await _edit_or_caption(call.message, B.HELP_TEXT, reply_markup=back_to_main())
    await call.answer()


# =============================================================================
#                            Модуль 06 · Обратная связь
# =============================================================================

@router.callback_query(F.data == "feedback:start")
async def cb_feedback_start(
    call: CallbackQuery,
    state: FSMContext,
    feedback: FeedbackStorage,
) -> None:
    user_id = call.from_user.id

    if not feedback.rating_ok(user_id, MAX_PER_HOUR):
        wait_sec = feedback.seconds_until_next(user_id)
        await call.answer(
            B.feedback_rate_limited(max(1, wait_sec // 60)),
            show_alert=True,
        )
        return

    await state.set_state(FeedbackStates.waiting_text)
    text = B.feedback_intro(MAX_PER_HOUR)
    await _edit_or_caption(call.message, text, reply_markup=cancel_menu())
    await call.answer()


@router.message(FeedbackStates.waiting_text, F.text)
async def handle_feedback_text(
    message: Message,
    state: FSMContext,
    feedback: FeedbackStorage,
    bot,
    cfg: Config,
) -> None:
    await state.clear()
    user_id = message.from_user.id
    body = (message.text or "").strip()

    if not (FEEDBACK_MIN_LEN <= len(body) <= FEEDBACK_MAX_LEN):
        await message.answer(
            B.feedback_invalid_length(), reply_markup=cancel_menu()
        )
        return

    if not feedback.rating_ok(user_id, MAX_PER_HOUR):
        wait_sec = feedback.seconds_until_next(user_id)
        await message.answer(
            B.feedback_rate_limited(max(1, wait_sec // 60)),
            reply_markup=back_to_main(),
        )
        return

    if not cfg.admin_ids:
        await message.answer(
            "К сожалению, администраторы недоступны. Попробуйте позже.",
            reply_markup=back_to_main(),
        )
        return

    first_ticket_id = 0
    sent_to = 0

    for admin_id in cfg.admin_ids:
        header = B.admin_feedback_header(
            ticket_id=0,  # заполним после сохранения
            user_id=user_id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
        )
        full_text = header + esc(body) + B.admin_feedback_footer()

        try:
            sent = await bot.send_message(
                admin_id,
                full_text,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning(
                "feedback: не доставлено админу %s: %s", admin_id, exc
            )
            continue

        record_id = feedback.save(
            user_id=user_id,
            admin_id=admin_id,
            admin_chat_id=sent.chat.id,
            admin_msg_id=sent.message_id,
            text=body,
        )
        if first_ticket_id == 0:
            first_ticket_id = record_id
        sent_to += 1

    if sent_to == 0:
        await message.answer(
            "Не удалось доставить обращение администраторам. Попробуйте позже.",
            reply_markup=back_to_main(),
        )
        return

    await message.answer(
        B.feedback_thanks(first_ticket_id or 0),
        reply_markup=back_to_main(),
    )
    logger.info(
        "feedback: user=%s ticket=#%s sent_to=%d",
        user_id, first_ticket_id, sent_to,
    )


@router.message(FeedbackStates.waiting_text)
async def feedback_wrong_input(message: Message) -> None:
    await message.answer(
        "Ожидается текстовое сообщение. Пришлите текст или нажмите «Отмена»."
    )


@router.message(F.reply_to_message)
async def handle_admin_reply(
    message: Message,
    state: FSMContext,
    feedback: FeedbackStorage,
    bot,
    cfg: Config,
) -> None:
    """
    Админ отвечает reply на сообщение-обращение — отправляем ответ
    пользователю. Если reply не на наше сообщение или отправитель не админ —
    пропускаем (SkipHandler), чтобы не мешать другим хендлерам.
    """
    if message.from_user is None or message.from_user.id not in cfg.admin_ids:
        raise SkipHandler

    reply = message.reply_to_message
    if reply is None:
        raise SkipHandler

    record = feedback.find_by_admin_msg(
        admin_chat_id=message.chat.id,
        admin_msg_id=reply.message_id,
    )
    if record is None:
        raise SkipHandler

    answer = (message.text or "").strip()
    if not answer:
        await message.reply("Пустой ответ — не отправляю.")
        return

    # Отправляем ответ пользователю.
    try:
        await bot.send_message(
            record.user_id,
            f"<b>ОТВЕТ ОТ АДМИНИСТРАЦИИ</b>\n"
            f"{B._DOT}\n"
            f"<i>по обращению #{record.id}</i>\n\n"
            f"{esc(answer)}",
            disable_web_page_preview=True,
        )
        feedback.mark_answered(record.id)
        await state.clear()
        await message.reply(
            f"✓ Ответ доставлен пользователю <code>{record.user_id}</code>."
        )
    except Exception as exc:
        logger.exception("feedback: не удалось ответить пользователю: %s", exc)
        await message.reply(
            "✗ Не удалось доставить ответ. Возможно, пользователь "
            "заблокировал бота."
        )


# =============================================================================
#                    Модуль 01 · Выбор режима
# =============================================================================

@router.callback_query(F.data == "gen:start")
async def cb_gen_start(call: CallbackQuery, cfg: Config, state: FSMContext) -> None:
    await state.clear()
    user_id = call.from_user.id
    rem = _remaining(user_id, cfg)
    if rem == 0:
        await call.answer(B.limit_reached(_limit(cfg)), show_alert=True)
        return
    is_new = _is_new_user(user_id, cfg)
    text = B.gen_mode_intro(user_id, rem, _limit(cfg), is_new=is_new)
    await _edit_or_caption(call.message, text, reply_markup=gen_mode_menu())
    await call.answer()


@router.callback_query(F.data == "gen:mode:smart")
async def cb_gen_smart(call: CallbackQuery, cfg: Config) -> None:
    user_id = call.from_user.id
    rem = _remaining(user_id, cfg)
    if rem == 0:
        await call.answer(B.limit_reached(_limit(cfg)), show_alert=True)
        return
    text = B.gen_count_intro("smart", rem, _limit(cfg))
    await _edit_or_caption(
        call.message, text,
        reply_markup=gen_count_menu("smart", cfg.gen_max_smart),
    )
    await call.answer()


@router.callback_query(F.data == "gen:mode:random")
async def cb_gen_random(call: CallbackQuery, cfg: Config) -> None:
    user_id = call.from_user.id
    rem = _remaining(user_id, cfg)
    if rem == 0:
        await call.answer(B.limit_reached(_limit(cfg)), show_alert=True)
        return
    text = B.gen_count_intro("random", rem, _limit(cfg))
    await _edit_or_caption(
        call.message, text,
        reply_markup=gen_count_menu("random", cfg.gen_max_random),
    )
    await call.answer()


@router.callback_query(F.data == "gen:mode:similar")
async def cb_gen_similar_start(
    call: CallbackQuery, cfg: Config, state: FSMContext,
) -> None:
    user_id = call.from_user.id
    rem = _remaining(user_id, cfg)
    if rem == 0:
        await call.answer(B.limit_reached(_limit(cfg)), show_alert=True)
        return
    await state.set_state(SimilarStates.waiting_base)
    text = B.gen_similar_intro(rem, _limit(cfg))
    await _edit_or_caption(call.message, text, reply_markup=cancel_menu())
    await call.answer()


@router.message(SimilarStates.waiting_base, F.text)
async def handle_similar_base(
    message: Message, state: FSMContext, cfg: Config,
) -> None:
    base = message.text.lstrip("@").strip().lower()
    if not base.isalpha() or not (3 <= len(base) <= SIMILAR_MAX_BASE_LEN):
        await message.answer(
            "Некорректное базовое имя. Нужны только латинские буквы, "
            f"3-{SIMILAR_MAX_BASE_LEN} символов."
        )
        return

    candidates = generate_similar(base, count=cfg.similar_max_results)
    if not candidates:
        await message.answer(
            f"По базе <code>{esc(base)}</code> не удалось сгенерировать варианты."
        )
        return

    await state.clear()
    rem = _remaining(message.from_user.id, cfg)
    text = B.gen_similar_pick_count(base, len(candidates), rem, _limit(cfg))
    await message.answer(
        text,
        reply_markup=gen_similar_count_menu(base, cfg.gen_max_smart),
    )


@router.callback_query(F.data == "gen:stop")
async def cb_gen_stop(call: CallbackQuery) -> None:
    stop_flags[call.from_user.id] = True
    await call.answer("Завершаю обработку…")


# =============================================================================
#                    Запуск генерации — роутеры
# =============================================================================

@router.callback_query(F.data.startswith("gen:run:"))
async def cb_gen_run(
    call: CallbackQuery,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    parts = call.data.split(":")
    if len(parts) != 4:
        await call.answer("Некорректный запрос", show_alert=True)
        return

    mode = parts[2]
    try:
        target = int(parts[3])
    except ValueError:
        await call.answer("Некорректный объём", show_alert=True)
        return

    if mode not in {"smart", "random"}:
        await call.answer("Некорректный режим", show_alert=True)
        return

    max_allowed = cfg.gen_max_smart if mode == "smart" else cfg.gen_max_random
    if target > max_allowed or target < 1:
        await call.answer(
            f"Допустимо от 1 до {max_allowed}.", show_alert=True
        )
        return

    await _run_generation(
        call=call, mode=mode, target=target, base=None,
        checker=checker, storage=storage, cfg=cfg,
    )


@router.callback_query(F.data.startswith("gen:sim:"))
async def cb_gen_similar_run(
    call: CallbackQuery,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    parts = call.data.split(":")
    if len(parts) != 4:
        await call.answer("Некорректный запрос", show_alert=True)
        return
    base = parts[2]
    try:
        target = int(parts[3])
    except ValueError:
        await call.answer("Некорректный объём", show_alert=True)
        return

    if not base.isalpha():
        await call.answer("Некорректная база", show_alert=True)
        return
    if target > cfg.gen_max_smart or target < 1:
        await call.answer(
            f"Допустимо от 1 до {cfg.gen_max_smart}.", show_alert=True
        )
        return

    await _run_generation(
        call=call, mode="similar", target=target, base=base,
        checker=checker, storage=storage, cfg=cfg,
    )


# =============================================================================
#                    Кнопки под отчётом
# =============================================================================

@router.callback_query(F.data.startswith("ack_claim:"))
async def cb_ack_claim(
    call: CallbackQuery,
    storage: FoundStorage,
    watchlist: Watchlist,
) -> None:
    username = call.data.split(":", 1)[1].strip().lower()
    logger.info("ack_claim:%s — user=%s", username, call.from_user.id)

    storage.mark_claimed(username)
    with contextlib.suppress(Exception):
        watchlist.remove(username)

    await call.answer(f"✓ @{username} отмечен как занятый")
    await call.message.answer(
        B.claim_ack_text(username),
        reply_markup=back_to_main(),
    )


@router.callback_query(F.data.startswith("sim_base:"))
async def cb_similar_from(
    call: CallbackQuery,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    base = call.data.split(":", 1)[1].strip().lower()
    if not base.isalpha() or not (3 <= len(base) <= SIMILAR_MAX_BASE_LEN):
        await call.answer("Некорректное имя", show_alert=True)
        return

    logger.info("sim_base:%s — user=%s", base, call.from_user.id)
    await call.answer("Готовлю похожие…")

    status = await call.message.answer(
        f"<b>Похожие на «{esc(base)}»</b>\n<i>Готовлю выборку…</i>"
    )

    await _run_generation(
        call=call, mode="similar", target=SIMILAR_QUICK_TARGET, base=base,
        checker=checker, storage=storage, cfg=cfg,
        status_message=status,
    )


# =============================================================================
#               Обёртка семафора + тело прогона
# =============================================================================

async def _run_generation(
    *,
    call: CallbackQuery,
    mode: str,
    target: int,
    base: str | None,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
    status_message: Message | None = None,
) -> None:
    status = status_message or call.message
    user_id = call.from_user.id

    sem = _gen_semaphore
    acquired = False
    if sem is not None:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.2)
            acquired = True
        except asyncio.TimeoutError:
            acquired = False
    else:
        acquired = True

    if not acquired:
        await _edit_or_caption(
            status,
            B.queue_wait_text(cfg.gen_busy_retry_hint),
            reply_markup=back_to_main(),
        )
        return

    try:
        await asyncio.wait_for(
            _run_generation_inner(
                status=status, user_id=user_id,
                mode=mode, target=target, base=base,
                checker=checker, storage=storage, cfg=cfg,
            ),
            timeout=GEN_HARD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(
            "Генерация user=%s превысила таймаут %d сек.",
            user_id, GEN_HARD_TIMEOUT,
        )
        stop_flags.pop(user_id, None)
        await _edit_or_caption(
            status,
            B.error_generic(
                "Обработка превысила максимальное время. "
                "Система принудительно завершила сеанс."
            ),
            reply_markup=back_to_main(),
        )
    finally:
        if sem is not None and acquired:
            sem.release()


async def _run_generation_inner(
    *,
    status: Message,
    user_id: int,
    mode: str,
    target: int,
    base: str | None,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    allowed, _ = _try_consume(user_id, cfg)
    if not allowed:
        await _edit_or_caption(
            status,
            B.limit_reached(_limit(cfg)),
            reply_markup=back_to_main(),
        )
        return

    stop_flags[user_id] = False

    found: list[tuple[str, int, str]] = []
    tested = 0
    attempts_cap = target * GEN_MAX_FACTOR
    last_edit = 0.0
    progress_lock = asyncio.Lock()

    def _next_batch(n: int) -> list[str]:
        if mode == "smart":
            return generate_many(n, length=5)
        if mode == "random":
            return generate_many_random(n, length=5)
        return generate_similar(base or "", count=n)

    async def _tick(force: bool = False) -> None:
        nonlocal last_edit
        async with progress_lock:
            now = time.monotonic()
            if not force and now - last_edit < PROGRESS_EDIT_INTERVAL:
                return
            last_edit = now
            text = B.search_progress(target, found, tested)
        await _edit_or_caption(
            status, text, reply_markup=gen_progress_kb(len(found))
        )

    async def _check_one(name: str) -> tuple[str, CheckResult | None]:
        nonlocal tested
        if stop_flags.get(user_id):
            return name, None
        try:
            res = await checker.check(name)
        finally:
            tested += 1
        asyncio.create_task(_tick())
        return name, res

    await _edit_or_caption(
        status, B.search_started(target, base=base),
        reply_markup=gen_progress_kb(0),
    )

    stopped_early = False
    seen_batch: set[str] = set()

    while len(found) < target and tested < attempts_cap:
        if stop_flags.get(user_id):
            stopped_early = True
            break

        batch = _next_batch(max(GEN_BATCH, (target - len(found)) * 2))
        found_names = {x[0] for x in found}
        batch = [b for b in batch if b not in seen_batch and b not in found_names]
        if not batch:
            continue
        seen_batch.update(batch)

        try:
            results = await asyncio.gather(*(_check_one(u) for u in batch))
        except AllSessionsFloodedError as exc:
            await _edit_or_caption(
                status, B.flooded_pause(exc.seconds_until_next),
                reply_markup=back_to_main(),
            )
            return

        for name, res in results:
            if res == "free":
                sc, why = score_username(name)
                if sc < cfg.filter_min_score:
                    continue
                found.append((name, sc, why))
                asyncio.create_task(_tick(force=True))
                if len(found) >= target:
                    break

        if stop_flags.get(user_id):
            stopped_early = True
            break

    rline = _usage_line(user_id, cfg)

    if not found:
        await _edit_or_caption(
            status,
            B.error_generic(
                f"Свободных позиций, удовлетворяющих фильтру "
                f"(оценка ≥ {cfg.filter_min_score}), не обнаружено. "
                f"Проверено {tested}."
            ) + f"\n\n{rline}",
            reply_markup=back_to_main(),
        )
        return

    _record_found(storage, found, source=mode, user_id=user_id)

    report = B.search_final(found, tested, stopped_early, rline)
    chunks = chunk_lines(report)
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        markup = found_actions_kb(found) if is_last else None
        await status.answer(
            chunk,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    await _edit_or_caption(
        status,
        f"<b>{'Обработка прервана' if stopped_early else 'Обработка завершена'}.</b>\n"
        f"Свободно: <b>{len(found)}</b>, проверено: {tested}.",
    )


# =============================================================================
#                    Модуль 02 · Пакетная проверка
# =============================================================================

@router.callback_query(F.data == "bulk:start")
async def cb_bulk_start(call: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    user_id = call.from_user.id
    rem = _remaining(user_id, cfg)
    if rem == 0:
        await call.answer(B.limit_reached(_limit(cfg)), show_alert=True)
        return
    await state.set_state(BulkStates.waiting_list)
    text = B.bulk_intro(rem, _limit(cfg), BULK_LIMIT)
    await _edit_or_caption(call.message, text, reply_markup=cancel_menu())
    await call.answer()


@router.message(BulkStates.waiting_list, F.text)
async def handle_bulk(
    message: Message,
    state: FSMContext,
    checker: TelegramChecker,
    storage: FoundStorage,
    cfg: Config,
) -> None:
    await state.clear()
    user_id = message.from_user.id

    names = _parse_usernames(message.text)
    if not names:
        await message.answer("Список не содержит позиций.")
        return
    if len(names) > BULK_LIMIT:
        await message.answer(
            f"Превышен объём: <b>{len(names)}</b>. Максимум {BULK_LIMIT}."
        )
        return

    allowed, _ = _try_consume(user_id, cfg)
    if not allowed:
        await message.answer(B.limit_reached(_limit(cfg)),
                             reply_markup=back_to_main())
        return

    total = len(names)
    status_msg = await message.answer(_progress_text(0, total, 0, 0))

    results: dict[str, CheckResult] = {}
    done = 0
    free_count = 0
    frag_count = 0
    last_edit = 0.0
    state_lock = asyncio.Lock()
    flooded_flag = False

    async def worker(name: str) -> None:
        nonlocal done, free_count, frag_count, last_edit, flooded_flag
        if stop_flags.get(user_id):
            return
        try:
            res = await checker.check(name)
        except AllSessionsFloodedError:
            flooded_flag = True
            return

        text_to_send: str | None = None
        async with state_lock:
            results[name] = res
            done += 1
            if res == "free":
                free_count += 1
            elif res == "fragment":
                frag_count += 1
            now = time.monotonic()
            if now - last_edit >= PROGRESS_EDIT_INTERVAL or done == total:
                last_edit = now
                text_to_send = _progress_text(done, total, free_count, frag_count)

        if text_to_send is not None:
            with contextlib.suppress(TelegramBadRequest):
                await status_msg.edit_text(text_to_send)

    await asyncio.gather(*(worker(n) for n in names))

    rline = _usage_line(user_id, cfg)
    if flooded_flag:
        await _edit_or_caption(
            status_msg,
            B.flooded_pause(checker._seconds_until_unpark()) + f"\n\n{rline}",
            reply_markup=back_to_main(),
        )
        return

    buckets = _split_results(results, names)
    for name in buckets["free"]:
        sc, _ = score_username(name)
        storage.record(name, sc, "bulk", "free", user_id)

    report = B.bulk_report(
        buckets["free"], buckets["fragment"], buckets["taken"],
        buckets["invalid"], buckets["error"], rline,
    )
    chunks = chunk_lines(report)
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        await message.answer(
            chunk,
            reply_markup=back_to_main() if is_last else None,
            disable_web_page_preview=True,
        )

    with contextlib.suppress(TelegramBadRequest):
        await status_msg.edit_text(
            f"<b>Обработка завершена.</b>  "
            f"Свободно: <b>{len(buckets['free'])}</b> из {total}."
        )


@router.message(BulkStates.waiting_list)
async def bulk_wrong_input(message: Message) -> None:
    await message.answer("Ожидается текстовый список: одно значение в строке.")


# =============================================================================
#                    Модуль 03 · Watchlist
# =============================================================================

@router.callback_query(F.data == "watch:start")
async def cb_watch_start(call: CallbackQuery, watchlist: Watchlist) -> None:
    if not watchlist.is_admin(call.from_user.id):
        await call.answer("Модуль доступен только операторам.", show_alert=True)
        return
    wb = watchlist.breakdown()
    text = B.watch_intro(watchlist.count(), wb)
    await _edit_or_caption(call.message, text, reply_markup=watch_menu())
    await call.answer()


@router.callback_query(F.data == "watch:list")
async def cb_watch_list(call: CallbackQuery, watchlist: Watchlist) -> None:
    if not watchlist.is_admin(call.from_user.id):
        await call.answer("Только операторы.", show_alert=True)
        return
    targets = watchlist.all_targets()
    if not targets:
        await _edit_or_caption(call.message, "<b>Список пуст.</b>",
                               reply_markup=watch_menu())
        await call.answer()
        return

    lines = [B.watch_list_header(len(targets)), ""]
    for name, status in targets[:120]:
        mark = B.STATUS_MARKS.get(status, "?")
        lines.append(f"  {mark}  <code>{esc(name)}</code>  ·  {status}")
    if len(targets) > 120:
        lines.append(f"\n<i>…и ещё {len(targets) - 120} позиций.</i>")

    for chunk in chunk_lines(lines):
        await call.message.answer(chunk, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "watch:add")
async def cb_watch_add(
    call: CallbackQuery, state: FSMContext, watchlist: Watchlist,
) -> None:
    if not watchlist.is_admin(call.from_user.id):
        await call.answer("Только операторы.", show_alert=True)
        return
    await state.set_state(WatchStates.waiting_add)
    await _edit_or_caption(
        call.message,
        "<b>МОДУЛЬ 03 · ДОБАВЛЕНИЕ</b>\n"
        "Направьте список имён (одно в строке).",
        reply_markup=cancel_menu(),
    )
    await call.answer()


@router.message(WatchStates.waiting_add, F.text)
async def handle_watch_add(
    message: Message, state: FSMContext, watchlist: Watchlist,
) -> None:
    await state.clear()
    names = _parse_usernames(message.text)
    if not names:
        await message.answer("Список пуст.")
        return
    added = 0
    for n in names:
        if not is_valid_username_format(n):
            continue
        if watchlist.count() >= WATCH_LIMIT:
            break
        if watchlist.add(n):
            added += 1
    await message.answer(
        f"<b>Добавлено:</b> {added} из {len(names)}.\n"
        f"Всего в списке: <b>{watchlist.count()}</b>",
        reply_markup=watch_menu(),
    )


@router.callback_query(F.data == "watch:del")
async def cb_watch_del(
    call: CallbackQuery, state: FSMContext, watchlist: Watchlist,
) -> None:
    if not watchlist.is_admin(call.from_user.id):
        await call.answer("Только операторы.", show_alert=True)
        return
    await state.set_state(WatchStates.waiting_del)
    await _edit_or_caption(
        call.message,
        "<b>МОДУЛЬ 03 · УДАЛЕНИЕ</b>\n"
        "Направьте список имён для удаления (одно в строке).",
        reply_markup=cancel_menu(),
    )
    await call.answer()


@router.message(WatchStates.waiting_del, F.text)
async def handle_watch_del(
    message: Message, state: FSMContext, watchlist: Watchlist,
) -> None:
    await state.clear()
    names = _parse_usernames(message.text)
    removed = sum(1 for n in names if watchlist.remove(n))
    await message.answer(
        f"<b>Удалено:</b> {removed} из {len(names)}.\n"
        f"Всего в списке: <b>{watchlist.count()}</b>",
        reply_markup=watch_menu(),
    )


# =============================================================================
#                    Модуль 04 · История находок
# =============================================================================

@router.callback_query(F.data == "found:show")
async def cb_found_show(
    call: CallbackQuery, storage: FoundStorage, cfg: Config,
) -> None:
    stats = storage.stats()
    text = B.found_intro(stats)
    await _edit_or_caption(call.message, text, reply_markup=found_menu())
    await call.answer()


@router.callback_query(F.data.startswith("found:list:"))
async def cb_found_list(
    call: CallbackQuery, storage: FoundStorage,
) -> None:
    key = call.data.split(":", 2)[2]
    if key == "30":
        days, label, limit = 30, "последние 30", 30
    elif key == "7d":
        days, label, limit = 7, "за 7 дней", 100
    elif key == "30d":
        days, label, limit = 30, "за 30 дней", 100
    else:
        await call.answer("Неизвестный фильтр", show_alert=True)
        return

    await _show_found_list(
        call.message, storage,
        user_id=call.from_user.id,
        days=days, limit=limit, label=label,
        as_new_message=False,
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
        text = "История пуста за указанный период."
        if as_new_message:
            await target.answer(text, reply_markup=back_to_main())
        else:
            await _edit_or_caption(target, text, reply_markup=found_menu())
        return

    label = label or f"последние {limit}"
    lines = [B.found_list_header(len(records), label), ""]
    for i, r in enumerate(records, 1):
        claimed_mark = "  ✓ занято" if r.claimed else ""
        when = time.strftime("%d.%m %H:%M", time.localtime(r.found_at))
        lines.append(
            f"  {i:>2}.  <code>{esc(r.username)}</code>  ·  "
            f"{r.score}/10  ·  {when}{claimed_mark}"
        )

    for chunk in chunk_lines(lines):
        if as_new_message:
            await target.answer(chunk, disable_web_page_preview=True,
                                reply_markup=found_menu())
        else:
            await target.answer(chunk, disable_web_page_preview=True)


@router.callback_query(F.data == "found:export")
async def cb_found_export(
    call: CallbackQuery, storage: FoundStorage,
) -> None:
    user_id = call.from_user.id
    await call.answer("Готовлю файл…")

    export_path = Path(f"found_export_{user_id}.csv")
    n = storage.export_csv(export_path, user_id=user_id, days=None)
    if n == 0:
        await call.message.answer(
            "В истории пока нет записей для экспорта.",
            reply_markup=found_menu(),
        )
        return

    try:
        await call.message.answer_document(
            FSInputFile(export_path, filename=f"userneed_found_{user_id}.csv"),
            caption=(
                f"<b>ЭКСПОРТ ИСТОРИИ</b>\n"
                f"{B._DOT}\n"
                f"Строк: <b>{n}</b>\n"
                f"Файл содержит: имя, оценку, статус, источник, дату."
            ),
            reply_markup=found_menu(),
        )
    except Exception as exc:
        logger.exception("Экспорт: не удалось отправить файл: %s", exc)
        await call.message.answer(
            "Не удалось отправить файл.", reply_markup=found_menu(),
        )
    finally:
        with contextlib.suppress(Exception):
            export_path.unlink(missing_ok=True)


# =============================================================================
#                              Fallback
# =============================================================================

@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Команда не распознана.\n\n·  /start — панель управления",
        reply_markup=main_menu(),
    )   