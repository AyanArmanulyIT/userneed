"""
Точка входа UserNeed.

Порядок старта:
  1. Конфиг (.env) → логи в data_dir/logs.
  2. Словари генератора.
  3. Хранилища: found, feedback, quota, check_cache, settings (все в data_dir).
  4. Telethon-пул → TelegramChecker.
  5. aiogram: Bot, SQLite FSM, middleware (доступ — outer), роутер.
  6. Фоновые сервисы: Watchlist, SessionGuard, DailyReporter, BackupManager.
  7. Polling; на выходе — аккуратно всё закрыть.

Все ресурсы регистрируются в AsyncExitStack сразу после создания, поэтому
закрываются и при штатной остановке, и если старт упал на полпути
(иначе, например, поток aiosqlite не даст процессу завершиться).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telethon import TelegramClient

from bot.handlers import (
    AccessMiddleware,
    LogAllMiddleware,
    init_gen_semaphore,
    router,
)
from config import PROJECT_ROOT, Config, load_config
from services.backup_manager import BackupManager
from services.check_cache import CheckCache
from services.checker import TelegramChecker
from services.daily_report import DailyReporter
from services.feedback import FeedbackStorage
from services.generator import dictionary_size, reload_external_dicts, stopwords_count
from services.quota import QuotaStorage
from services.runtime_settings import RuntimeSettings
from services.session_guard import SessionGuard
from services.sqlite_fsm import SQLiteStorage
from services.storage import FoundStorage
from services.watchlist import Watchlist

logger = logging.getLogger("userneed")

USER_COMMANDS = [
    BotCommand(command="start", description="Панель управления"),
    BotCommand(command="help", description="Регламент"),
    BotCommand(command="found", description="История находок"),
    BotCommand(command="cancel", description="Сбросить текущий ввод"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="params", description="Параметры прогонов"),
    BotCommand(command="stats", description="Состояние сервиса"),
    BotCommand(command="sessions", description="Состояние сессий"),
    BotCommand(command="today", description="Статистика за сутки"),
    BotCommand(command="feedback", description="Последние обращения"),
    BotCommand(command="quota", description="Квота пользователя: /quota <id>"),
    BotCommand(command="quota_reset", description="Сбросить квоту: /quota_reset <id>"),
    BotCommand(command="quota_add", description="Добавить сеансы: /quota_add <id> <n>"),
    BotCommand(command="report_now", description="Сводка сейчас"),
    BotCommand(command="backup_now", description="Бэкап сейчас"),
]


# =============================================================================
#                        Логи с ротацией
# =============================================================================

def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "userneed.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler], force=True)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


# =============================================================================
#                        Telethon-пул
# =============================================================================

async def _start_one_client(name: str, cfg: Config) -> TelegramClient | None:
    """
    Подключает одну сессию. Неавторизованную — пропускает с подсказкой:
    интерактивный логин в главном процессе (без терминала) просто повис бы,
    для этого есть qr_login.py.
    """
    client = TelegramClient(cfg.session_path(name), cfg.api_id, cfg.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(
                "Сессия %r не авторизована. Выполните: python qr_login.py %s", name, name,
            )
            await client.disconnect()
            return None
        me = await client.get_me()
        handle = f"@{me.username}" if getattr(me, "username", None) else me.first_name
        logger.info("Сессия %r подключена — %s (id=%s)", name, handle, me.id)
        return client
    except (OSError, asyncio.TimeoutError) as exc:
        logger.error("Сессия %r: нет связи с Telegram (%s)", name, exc)
    except Exception as exc:  # noqa: BLE001 — одна упавшая сессия не должна ронять остальные
        logger.exception("Сессия %r не поднялась: %s", name, exc)
    with contextlib.suppress(Exception):
        await client.disconnect()
    return None


async def _start_all_clients(cfg: Config) -> tuple[list[TelegramClient], list[str]]:
    clients: list[TelegramClient] = []
    names: list[str] = []
    for name in cfg.session_names:
        c = await _start_one_client(name, cfg)
        if c is not None:
            clients.append(c)
            names.append(name)
    return clients, names


async def _disconnect_all(clients: list[TelegramClient]) -> None:
    for c in clients:
        with contextlib.suppress(Exception):
            await c.disconnect()


async def _set_commands(bot: Bot, cfg: Config) -> None:
    """Меню команд: всем — пользовательские, админам — расширенное."""
    with contextlib.suppress(Exception):
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in cfg.admin_ids:
        with contextlib.suppress(Exception):
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id),
            )


# =============================================================================
#                              main
# =============================================================================

async def main() -> None:
    try:
        cfg = load_config()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        print("Проверьте .env (образец — .env.example).", file=sys.stderr)
        raise SystemExit(1) from exc

    setup_logging(cfg.log_dir)
    logger.info("Корень проекта: %s  ·  данные: %s", PROJECT_ROOT, cfg.data_dir)
    for warning in cfg.runtime_warnings():
        logger.warning("⚠ %s", warning)

    # --- Словари ---
    reload_external_dicts(cfg.use_external_dict)
    words, names_cnt = dictionary_size()
    logger.info(
        "Словари: %d слов, %d имён, %d стоп-слов (внешние: %s)",
        words, names_cnt, stopwords_count(), "вкл" if cfg.use_external_dict else "выкл",
    )

    async with contextlib.AsyncExitStack() as stack:
        # --- Хранилища ---
        if cfg.storage_enabled:
            storage = FoundStorage(
                path=cfg.db_path("found.db"), retention_days=cfg.found_history_days,
            )
            storage.cleanup()
        else:
            storage = FoundStorage(path=":memory:", retention_days=1)
            logger.info("STORAGE_ENABLED=0 — история не сохраняется.")
        stack.callback(storage.close)

        feedback = FeedbackStorage(path=cfg.db_path("feedback.db"), retention_days=90)
        stack.callback(feedback.close)
        removed_fb = feedback.cleanup()
        if removed_fb:
            logger.info("Обращения: очищено %d старых", removed_fb)

        quota = QuotaStorage(path=cfg.db_path("quota.db"), limit=cfg.usage_limit)
        stack.callback(quota.close)
        check_cache = CheckCache(path=cfg.db_path("check_cache.db"))
        stack.callback(check_cache.close)

        # Переопределения параметров из панели /params. Читаются на каждом
        # прогоне; чекер получает новые значения через bind_checker().
        runtime = RuntimeSettings(path=cfg.db_path("settings.db"), cfg=cfg)
        stack.callback(runtime.close)
        if runtime.override_count():
            logger.info(
                "Панель параметров: %d переопределений из settings.db", runtime.override_count(),
            )

        # --- Telethon ---
        clients, names = await _start_all_clients(cfg)
        stack.push_async_callback(_disconnect_all, clients)
        if not clients:
            raise RuntimeError(
                "Ни одну Telethon-сессию поднять не удалось. "
                "Авторизуйте сессии: python qr_login.py <имя>"
            )
        if len(clients) < len(cfg.session_names):
            logger.warning("Поднялось %d сессий из %d.", len(clients), len(cfg.session_names))
        logger.info("Round-Robin-пул готов: %d сессий (%s)", len(clients), ", ".join(names))

        checker = TelegramChecker(
            clients=clients,
            session_names=names,
            concurrency=runtime.check_concurrency,
            retries=runtime.check_retries,
            flood_extra=cfg.flood_cooldown_extra,
            park_threshold=cfg.flood_park_threshold,
            delay_min=runtime.check_delay_min,
            delay_max=runtime.check_delay_max,
            http_precheck_enabled=runtime.http_precheck,
            cache=check_cache,
        )
        await checker.start()
        stack.push_async_callback(checker.aclose)
        runtime.bind_checker(checker)

        # --- aiogram ---
        bot = Bot(
            token=cfg.bot_token,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        stack.push_async_callback(bot.session.close)

        fsm_storage = SQLiteStorage(path=cfg.db_path("fsm.db"))
        stack.push_async_callback(fsm_storage.close)
        removed_fsm = await fsm_storage.cleanup(max_age_days=7)
        if removed_fsm:
            logger.info("FSM: очищено %d устаревших состояний", removed_fsm)

        dp = Dispatcher(storage=fsm_storage)
        dp.include_router(router)

        # Доступ — OUTER middleware: срабатывает до фильтров, обойти нельзя.
        access_mw = AccessMiddleware(cfg)
        dp.message.outer_middleware(access_mw)
        dp.callback_query.outer_middleware(access_mw)
        log_mw = LogAllMiddleware()
        dp.message.middleware(log_mw)
        dp.callback_query.middleware(log_mw)

        init_gen_semaphore(cfg.global_gen_concurrency)

        logger.info(
            "Доступ: %s  ·  приглашено: %d  ·  админов: %d  ·  лимит: %d  ·  семафор: %d",
            "закрытая бета" if cfg.beta_mode else "открытый",
            len(cfg.allowed_user_ids), len(cfg.admin_ids),
            cfg.usage_limit, cfg.global_gen_concurrency,
        )

        # --- Фоновые сервисы (стоп — в обратном порядке регистрации) ---
        watchlist = Watchlist(
            checker=checker,
            bot=bot,
            admin_ids=cfg.admin_ids,
            hot_interval=cfg.watch_hot_interval,
            warm_interval=cfg.watch_warm_interval,
            cold_interval=cfg.watch_cold_interval,
            batch_size=cfg.watch_batch_size,
            sleep_after_days=cfg.watch_sleep_after_days,
            path=cfg.db_path("watchlist.db"),
        )
        watchlist.start()
        stack.push_async_callback(watchlist.stop)

        guard = SessionGuard(
            clients=clients, session_names=names, bot=bot, admin_ids=cfg.admin_ids,
        )
        guard.start()
        stack.push_async_callback(guard.stop)

        reporter = DailyReporter(
            bot=bot,
            admin_ids=cfg.admin_ids,
            checker=checker,
            storage=storage,
            watchlist=watchlist,
            guard=guard,
            state_file=cfg.data_dir / "last_report.txt",
        )
        reporter.start()
        stack.push_async_callback(reporter.stop)

        backup = BackupManager(
            bot=bot,
            admin_ids=cfg.admin_ids,
            project_root=PROJECT_ROOT,
            data_dir=cfg.data_dir,
            backup_dir=cfg.backup_dir,
            include_secrets=cfg.backup_include_secrets,
        )
        backup.start()
        stack.push_async_callback(backup.stop)

        await _set_commands(bot, cfg)
        logger.info("UserNeed запущен, слушаю обновления…")

        try:
            await dp.start_polling(
                bot,
                checker=checker,
                watchlist=watchlist,
                storage=storage,
                feedback=feedback,
                quota=quota,
                check_cache=check_cache,
                settings=runtime,
                guard=guard,
                reporter=reporter,
                backup_mgr=backup,
                cfg=cfg,
                allowed_updates=dp.resolve_used_update_types(),
            )
        finally:
            logger.info("Останавливаюсь…")

    logger.info("Остановлен чисто. До связи.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Получен сигнал остановки")
