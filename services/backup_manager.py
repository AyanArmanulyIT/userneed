"""
BackupManager — суточные бэкапы ключевых файлов проекта.

Что кладёт в архив:
  · found.db       — история находок
  · watchlist.db   — список отслеживаемых
  · fsm.db         — состояния FSM
  · *.session      — авторизации Telethon (КРИТИЧНО)
  · .env           — конфиг с токенами
  · assets/        — баннер, словари

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

BACKUP_DIR = Path("backups")
BACKUP_INTERVAL = 86400      # раз в сутки
RETENTION_DAYS = 14
MAX_BACKUPS = 20

_LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


class BackupManager:
    def __init__(
        self,
        bot: Bot,
        admin_ids: tuple[int, ...] | frozenset[int],
        project_root: Path | None = None,
    ) -> None:
        self._bot = bot
        self._admins = tuple(admin_ids)
        self._root = project_root or Path.cwd()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "BackupManager запущен (интервал %d ч)",
                BACKUP_INTERVAL // 3600,
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        # Даём боту стартовать, потом сразу первый бэкап,
        # затем — раз в сутки.
        await asyncio.sleep(300)
        while True:
            try:
                info = await asyncio.to_thread(self.make_backup)
                await self._notify(info)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("BackupManager: ошибка в цикле")
            await asyncio.sleep(BACKUP_INTERVAL)

    def make_backup(self) -> dict:
        """Синхронный метод — запускается через asyncio.to_thread."""
        BACKUP_DIR.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        archive_path = BACKUP_DIR / f"backup_{ts}.zip"
        tmp_dir = BACKUP_DIR / f"_tmp_{ts}"
        tmp_dir.mkdir(exist_ok=True)

        files_added = 0
        try:
            # 1) SQLite-базы
            for name in ("found.db", "watchlist.db", "fsm.db"):
                src = self._root / name
                if not src.is_file():
                    continue
                self._copy_sqlite(src, tmp_dir / name)
                files_added += 1

            # 2) Session-файлы Telethon
            for src in self._root.glob("*.session"):
                with contextlib.suppress(Exception):
                    self._copy_sqlite(src, tmp_dir / src.name)
                    files_added += 1

            # 3) .env
            env_file = self._root / ".env"
            if env_file.is_file():
                shutil.copy2(env_file, tmp_dir / ".env")
                files_added += 1

            # 4) assets/
            assets_dir = self._root / "assets"
            if assets_dir.is_dir():
                dst_assets = tmp_dir / "assets"
                shutil.copytree(assets_dir, dst_assets, dirs_exist_ok=True)
                files_added += sum(
                    1 for f in dst_assets.rglob("*") if f.is_file()
                )

            # 5) Упаковка
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in tmp_dir.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(tmp_dir))
        finally:
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir)

        size_mb = archive_path.stat().st_size / (1024 * 1024)
        removed = self._rotate()

        return {
            "path": str(archive_path),
            "size_mb": round(size_mb, 2),
            "files": files_added,
            "removed": removed,
        }

    @staticmethod
    def _copy_sqlite(src: Path, dst: Path) -> None:
        """Консистентная копия SQLite через .backup()."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Открываем read-only, чтобы не блокировать основную программу.
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
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

        # По возрасту.
        old = [
            p for p in BACKUP_DIR.glob("backup_*.zip")
            if p.stat().st_mtime < cutoff
        ]
        for p in old:
            with contextlib.suppress(Exception):
                p.unlink()
                removed += 1

        # По количеству.
        remaining = sorted(
            BACKUP_DIR.glob("backup_*.zip"),
            key=lambda p: p.stat().st_mtime,
        )
        if len(remaining) > MAX_BACKUPS:
            for p in remaining[: len(remaining) - MAX_BACKUPS]:
                with contextlib.suppress(Exception):
                    p.unlink()
                    removed += 1

        return removed

    async def _notify(self, info: dict) -> None:
        text = (
            f"<b>BACKUP · ВЫПОЛНЕН</b>\n"
            f"{_LINE}\n\n"
            f"файлов в архиве: <b>{info['files']}</b>\n"
            f"размер: <b>{info['size_mb']} МБ</b>\n"
            f"путь: <code>{info['path']}</code>"
        )
        if info["removed"]:
            text += f"\n\n<i>удалено старых: {info['removed']}</i>"

        for admin_id in self._admins:
            try:
                await self._bot.send_message(
                    admin_id, text, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception(
                    "BackupManager: не доставлено админу %s", admin_id
                )