"""
Точка входа UserNeed.
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
from telethon import TelegramClient

from bot.handlers import (
    AccessMiddleware,
    LogAllMiddleware,
    init_gen_semaphore,
    router,
)
from config import Config, load_config
from services.backup_manager import BackupManager
from services.check_cache import CheckCache
from services.checker import TelegramChecker
from services.daily_report import DailyReporter
from services.feedback import FeedbackStorage
from services.generator import reload_external_dicts
from services.session_guard import SessionGuard
from services.sqlite_fsm import SQLiteStorage
from services.storage import FoundStorage
from services.watchlist import Watchlist

# =============================================================================
#                        Логи с ротацией
# =============================================================================

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "userneed.log"

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s"
))

_file = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_file.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s"
))

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

logger = logging.getLogger("userneed")


async def _start_one_client(name: str, cfg: Config) -> TelegramClient | None:
    client = TelegramClient(name, cfg.api_id, cfg.api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.info("Сессия %r не авторизована — требуется вход.", name)
            await client.start()
        me = await client.get_me()
        handle = f"@{me.username}" if getattr(me, "username", None) else me.first_name
        logger.info("Сессия %r подключена — %s (id=%s)", name, handle, me.id)
        return client
    except Exception as exc:
        logger.exception("Сессия %r не поднялась: %s", name, exc)
        with contextlib.suppress(Exception):
            await client.disconnect()
        return None


async def _start_all_clients(
    cfg: Config,
) -> tuple[list[TelegramClient], list[str]]:
    clients: list[TelegramClient] = []
    names: list[str] = []
    for name in cfg.session_names:
        c = await _start_one_client(name, cfg)
        if c is not None:
            clients.append(c)
            names.append(name)
    return clients, names


async def main() -> None:
    cfg = load_config()

    reload_external_dicts(cfg.use_external_dict)
    logger.info("Словари загружены (внешние: %s)",
                "вкл" if cfg.use_external_dict else "выкл")

    # --- Хранилище находок ---
    if cfg.storage_enabled:
        storage = FoundStorage(
            path=Path("found.db"),
            retention_days=cfg.found_history_days,
        )
        removed = storage.cleanup()
        if removed:
            logger.info("Хранилище: очищено %d старых записей", removed)
    else:
        storage = FoundStorage(path=":memory:", retention_days=1)
        logger.info("STORAGE_ENABLED=0 — история не сохраняется.")

    # --- Хранилище обращений ---
    feedback = FeedbackStorage(path=Path("feedback.db"), retention_days=90)
    removed_fb = feedback.cleanup()
    if removed_fb:
        logger.info("Обращения: очищено %d старых", removed_fb)

    # --- Кэш проверок ---
    check_cache = CheckCache(path=Path("check_cache.db"))

    # --- Telethon ---
    clients, names = await _start_all_clients(cfg)
    if not clients:
        raise RuntimeError("Ни одну Telethon-сессию поднять не удалось.")
    if len(clients) == 1 and len(cfg.session_names) > 1:
        logger.warning("Поднялась только одна сессия из %d.",
                       len(cfg.session_names))
    logger.info("Round-Robin-пул готов: %d сессий (%s)",
                len(clients), ", ".join(names))

    # --- Чекер с кэшем ---
    checker = TelegramChecker(
        clients=clients,
        session_names=names,
        concurrency=cfg.check_concurrency,
        flood_extra=cfg.flood_cooldown_extra,
        park_threshold=cfg.flood_park_threshold,
        delay_min=cfg.check_delay_min,
        delay_max=cfg.check_delay_max,
        http_precheck_enabled=cfg.http_precheck_enabled,
        cache=check_cache,
    )
    await checker.start()

    # --- aiogram ---
    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    fsm_storage = SQLiteStorage(path="fsm.db")
    dp = Dispatcher(storage=fsm_storage)
    dp.include_router(router)

    access_mw = AccessMiddleware(cfg)
    log_mw = LogAllMiddleware()
    dp.callback_query.middleware(access_mw)
    dp.message.middleware(access_mw)
    dp.callback_query.middleware(log_mw)
    dp.message.middleware(log_mw)

    init_gen_semaphore(cfg.global_gen_concurrency)

    logger.info(
        "Доступ: %s  ·  приглашено: %d  ·  лимит: %d  ·  семафор: %d",
        "закрытая бета" if cfg.beta_mode else "открытый",
        len(cfg.allowed_user_ids),
        cfg.usage_limit,
        cfg.global_gen_concurrency,
    )

    # --- Watchlist ---
    watchlist = Watchlist(
        checker=checker,
        bot=bot,
        admin_ids=cfg.admin_ids,
        hot_interval=cfg.watch_hot_interval,
        warm_interval=cfg.watch_warm_interval,
        cold_interval=cfg.watch_cold_interval,
        batch_size=cfg.watch_batch_size,
        sleep_after_days=cfg.watch_sleep_after_days,
    )
    watchlist.start()

    # --- Session Guard ---
    guard = SessionGuard(
        clients=clients,
        session_names=names,
        bot=bot,
        admin_ids=cfg.admin_ids,
    )
    guard.start()

    # --- DailyReporter ---
    reporter = DailyReporter(
        bot=bot,
        admin_ids=cfg.admin_ids,
        checker=checker,
        storage=storage,
        watchlist=watchlist,
        guard=guard,
    )
    reporter.start()

    # --- BackupManager ---
    backup = BackupManager(
        bot=bot,
        admin_ids=cfg.admin_ids,
        project_root=Path.cwd(),
    )
    backup.start()

    logger.info("UserNeed запущен, слушаю обновления…")

    try:
        await dp.start_polling(
            bot,
            checker=checker,
            watchlist=watchlist,
            storage=storage,
            feedback=feedback,
            guard=guard,
            reporter=reporter,
            backup_mgr=backup,
            cfg=cfg,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        logger.info("Останавливаюсь…")
        with contextlib.suppress(Exception):
            await reporter.stop()
        with contextlib.suppress(Exception):
            await backup.stop()
        with contextlib.suppress(Exception):
            await guard.stop()
        with contextlib.suppress(Exception):
            await watchlist.stop()
        with contextlib.suppress(Exception):
            await checker.aclose()
        for c in clients:
            with contextlib.suppress(Exception):
                await c.disconnect()
        with contextlib.suppress(Exception):
            storage.close()
        with contextlib.suppress(Exception):
            feedback.close()
        with contextlib.suppress(Exception):
            check_cache.close()
        with contextlib.suppress(Exception):
            await fsm_storage.close()
        with contextlib.suppress(Exception):
            await bot.session.close()
        logger.info("Остановлен чисто. До связи.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки")