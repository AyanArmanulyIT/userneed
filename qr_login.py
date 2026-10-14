"""
QR-логин в Telethon.

Не просит ни SMS, ни код в приложение — сканируете QR с уже залогиненного
телефона. Обходит любые проблемы доставки кода.

Файл сессии кладётся туда же, где его ищет main.py (cfg.session_path),
то есть в DATA_DIR (по умолчанию — корень проекта).

Запуск:  python qr_login.py [имя_сессии]
"""
from __future__ import annotations

import asyncio
import logging
import sys

from telethon import TelegramClient

from config import load_config

try:
    import qrcode
except ImportError:  # pragma: no cover
    print("Не установлен пакет qrcode: pip install -r requirements.txt")
    raise SystemExit(1)

logging.basicConfig(level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)

QR_TIMEOUT = 120


async def main() -> None:
    cfg = load_config()

    print("Имена сессий из .env:", ", ".join(cfg.session_names))
    if len(sys.argv) > 1:
        session_name = sys.argv[1].strip()
    else:
        session_name = input(
            f"Какое имя логиним? [{cfg.session_names[0]}]: "
        ).strip() or cfg.session_names[0]

    if session_name not in cfg.session_names:
        print(f"⚠ {session_name!r} нет в SESSION_NAMES — main.py эту сессию не подхватит.")

    client = TelegramClient(cfg.session_path(session_name), cfg.api_id, cfg.api_hash)
    await client.connect()

    try:
        # Уже залогинена — выходим сразу, чтобы не плодить запросы.
        if await client.is_user_authorized():
            me = await client.get_me()
            handle = f"@{me.username}" if me.username else me.first_name
            print(f"\nℹ️  Сессия {session_name!r} уже авторизована как {handle} (id={me.id}).")
            print(f"   Чтобы перелогиниться — удалите {cfg.session_path(session_name)}.session "
                  f"и запустите снова.")
            return

        print(f"\n✅ Подключился к Telegram, готовлю QR для сессии {session_name!r}…\n")

        # qr_login() — корутина, возвращает QRLogin с .url и .wait().
        qr = await client.qr_login()

        filename = f"qr_{session_name}.png"
        qrcode.make(qr.url).save(filename)

        print("=" * 60)
        print("📱  ШАГИ ДЛЯ ТЕЛЕФОНА:")
        print("=" * 60)
        print("1. Откройте Telegram на телефоне (тот аккаунт, который логиним)")
        print("2. Настройки → Устройства → «Подключить устройство»")
        print("3. Наведите камеру на QR-файл:")
        print(f"   {filename}")
        print("=" * 60)
        print(f"⏳  Жду сканирования… (QR живёт ~{QR_TIMEOUT} секунд)")
        print()

        try:
            # wait() сам пересоздаёт токен на лету, пока не истечёт общий timeout.
            await qr.wait(timeout=QR_TIMEOUT)
        except asyncio.TimeoutError:
            print("\n⌛ QR истёк. Просто запустите скрипт снова.")
            return
        except Exception as exc:  # noqa: BLE001 — показываем причину и выходим
            print(f"\n💥 Ошибка при ожидании QR: {type(exc).__name__}: {exc}")
            return

        me = await client.get_me()
        handle = f"@{me.username}" if me.username else me.first_name
        print(f"\n🎉 Готово! Залогинен как: {me.first_name} ({handle}, id={me.id})")
        print(f"📁 Файл сессии: {cfg.session_path(session_name)}.session")
        print("   main.py подхватит его автоматически.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
