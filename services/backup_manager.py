"""
BackupManager — суточные бэкапы ключевых файлов проекта.

Что кладёт в архив (всегда):
  · found.db, watchlist.db, fsm.db, feedback.db, quota.db, check_cache.db
  · assets/ — баннер, словари

Что кладёт ТОЛЬКО при include_secrets=True (BACKUP_INCLUDE_SECRETS=1):
  · *.session — авторизации Telethon = полный доступ к аккаунтам
  · .env      — токен бота, API_HASH

По умолчанию секреты исключены: zip не шифруется, и утечка архива
(переслали в чат, оставили на сервере) означала бы захват аккаунтов.
Сессии и .env восстанавливаются вручную (qr_login.py + ваш .env).

SQLite-файлы копируются через штатный .backup() — гарантирует консистентный
снимок даже при активной записи.

Ротация: архивы старше RETENTION_DAYS или сверх MAX_BACKUPS удаляются.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode

logger = logging.getLogger(__name__)

BACKUP_INTERVAL = 86400      # раз в сутки
STARTUP_DELAY = 300          # первый бэкап через 5 минут после старта
RETENTION_DAYS = 14
MAX_BACKUPS = 20

DB_FILES = (
    "found.db", "watchlist.db", "fsm.db",
    "feedback.db", "quota.db", "check_cache.db",
)

_LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


class BackupManager:
    def __init__(
        self,
        bot: Bot,
        admin_ids: tuple[int, ...] | frozenset[int],
        project_root: Path | None = None,
        *,
        data_dir: Path | None = None,
        backup_dir: Path | None = None,
        include_secrets: bool = False,
    ) -> None:
        self._bot = bot
        self._admins = tuple(admin_ids)
        self._root = project_root or Path.cwd()
        self._data = data_dir or self._root
        self._backup_dir = backup_dir or (self._data / "backups")
        self._include_secrets = include_secrets
        self._task: asyncio.Task | None = None

    @property
    def include_secrets(self) -> bool:
        return self._include_secrets

    # --- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="backup")
            logger.info(
                "BackupManager запущен (интервал %d ч, секреты: %s, папка: %s)",
                BACKUP_INTERVAL // 3600,
                "включены" if self._include_secrets else "исключены",
                self._backup_dir,
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            try:
                info = await asyncio.to_thread(self.make_backup)
                await self._notify(info)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                logger.exception("BackupManager: ошибка в цикле")
            await asyncio.sleep(BACKUP_INTERVAL)

    # --- Бэкап ---------------------------------------------------------------

    def make_backup(self) -> dict:
        """Синхронный метод — запускается через asyncio.to_thread."""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        archive_path = self._backup_dir / f"backup_{ts}.zip"
        tmp_dir = self._backup_dir / f"_tmp_{ts}"
        tmp_dir.mkdir(exist_ok=True)

        files_added = 0
        skipped: list[str] = []
        try:
            # 1) SQLite-базы
            for name in DB_FILES:
                src = self._data / name
                if not src.is_file():
                    continue
                try:
                    self._copy_sqlite(src, tmp_dir / name)
                    files_added += 1
                except sqlite3.Error as exc:
                    logger.warning("Backup: %s пропущен (%s)", name, exc)
                    skipped.append(name)

            # 2) Секреты — только по явному разрешению
            if self._include_secrets:
                for src in self._data.glob("*.session"):
                    try:
                        self._copy_sqlite(src, tmp_dir / src.name)
                        files_added += 1
                    except sqlite3.Error as exc:
                        logger.warning("Backup: %s пропущен (%s)", src.name, exc)
                        skipped.append(src.name)
                env_file = self._root / ".env"
                if env_file.is_file():
                    shutil.copy2(env_file, tmp_dir / ".env")
                    files_added += 1

            # 3) assets/
            assets_dir = self._root / "assets"
            if assets_dir.is_dir():
                dst_assets = tmp_dir / "assets"
                shutil.copytree(assets_dir, dst_assets, dirs_exist_ok=True)
                files_added += sum(1 for f in dst_assets.rglob("*") if f.is_file())

            # 4) Упаковка
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(tmp_dir.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(tmp_dir))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        size_mb = archive_path.stat().st_size / (1024 * 1024)
        removed = self._rotate()

        logger.info(
            "Backup: %s — %d файлов, %.2f МБ, удалено старых: %d",
            archive_path.name, files_added, size_mb, removed,
        )
        return {
            "path": str(archive_path),
            "size_mb": round(size_mb, 2),
            "files": files_added,
            "removed": removed,
            "skipped": skipped,
            "secrets_included": self._include_secrets,
        }

    @staticmethod
    def _copy_sqlite(src: Path, dst: Path) -> None:
        """Консистентная копия SQLite через .backup()."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Открываем read-only, чтобы не блокировать основную программу.
        # as_uri() даёт корректный file:///… и на Windows, и на POSIX.
        src_con = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
        try:
            dst_con = sqlite3.connect(dst)
            try:
                with dst_con:
                    src_con.backup(dst_con)
            finally:
                dst_con.close()
        finally:
            src_con.close()

    def _rotate(self) -> int:
        removed = 0
        cutoff = time.time() - RETENTION_DAYS * 86400

        archives = sorted(
            self._backup_dir.glob("backup_*.zip"),
            key=lambda p: p.stat().st_mtime,
        )

        # По возрасту.
        keep: list[Path] = []
        for p in archives:
            if p.stat().st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    p.unlink()
                    removed += 1
                    continue
            keep.append(p)

        # По количеству.
        if len(keep) > MAX_BACKUPS:
            for p in keep[: len(keep) - MAX_BACKUPS]:
                with contextlib.suppress(OSError):
                    p.unlink()
                    removed += 1

        return removed

    # --- Уведомление ---------------------------------------------------------

    async def _notify(self, info: dict) -> None:
        secrets = (
            "<b>включены</b>" if info.get("secrets_included")
            else "исключены"
        )
        text = (
            f"<b>BACKUP · ВЫПОЛНЕН</b>\n"
            f"{_LINE}\n\n"
            f"файлов в архиве: <b>{info['files']}</b>\n"
            f"размер: <b>{info['size_mb']} МБ</b>\n"
            f"секреты: {secrets}\n"
            f"путь: <code>{info['path']}</code>"
        )
        if info.get("skipped"):
            text += f"\n\n<i>пропущено: {', '.join(info['skipped'])}</i>"
        if info.get("removed"):
            text += f"\n\n<i>удалено старых: {info['removed']}</i>"

        for admin_id in self._admins:
            try:
                await self._bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception("BackupManager: не доставлено админу %s", admin_id)
