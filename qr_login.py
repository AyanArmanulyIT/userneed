"""
QR-логин в Telethon.

Не просит ни SMS, ни код в приложение — сканируешь QR со своего
уже залогиненного телефона. Обходит любые проблемы доставки кода.
"""
import asyncio
import logging

import qrcode
from telethon import TelegramClient

from config import load_config

logging.basicConfig(level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)


async def main() -> None:
    cfg = load_config()

    print("Имена сессий из .env:", ", ".join(cfg.session_names))
    session_name = input("Какое имя логиним? (например, acc1): ").strip()
    if not session_name:
        session_name = cfg.session_names[0]

    client = TelegramClient(session_name, cfg.api_id, cfg.api_hash)
    await client.connect()

    # Если сессия уже залогинена — выходим сразу, чтобы не плодить запросы.
    if await client.is_user_authorized():
        me = await client.get_me()
        handle = f"@{me.username}" if me.username else me.first_name
        print(f"\nℹ️  Сессия {session_name!r} уже авторизована как {handle} (id={me.id}).")
        print(f"   Хочешь перелогиниться — удали {session_name}.session и запусти снова.")
        await client.disconnect()
        return

    print(f"\n✅ Подключился к Telegram, готовлю QR для сессии {session_name!r}…\n")

    # 🔑 ГЛАВНЫЙ ФИКС: qr_login() — это КОРУТИНА, её нужно await-ить.
    # Возвращает объект QRLogin с полями .url и .wait(). Никакого `async with`.
    qr = await client.qr_login()

    filename = f"qr_{session_name}.png"
    qrcode.make(qr.url).save(filename)

    print("=" * 60)
    print("📱  ШАГИ ДЛЯ ТЕЛЕФОНА:")
    print("=" * 60)
    print("1. Открой Telegram на телефоне (тот аккаунт, который логиним)")
    print("2. Настройки → Устройства → «Подключить устройство»")
    print(f"3. Наведи камеру на QR-файл:")
    print(f"   {filename}")
    print("=" * 60)
    print("⏳  Жду сканирования… (QR живёт ~2 минуты)")
    print()

    try:
        # wait() сам пересоздаёт токен на лету, пока не истечёт общий timeout.
        await qr.wait(timeout=120)
    except asyncio.TimeoutError:
        print("\n⌛ QR истёк. Просто запусти скрипт снова.")
        await client.disconnect()
        return
    except Exception as exc:
        print(f"\n💥 Ошибка при ожидании QR: {type(exc).__name__}: {exc}")
        await client.disconnect()
        return

    me = await client.get_me()
    handle = f"@{me.username}" if me.username else me.first_name
    print(f"\n🎉 Готово! Залогинен как: {me.first_name} ({handle}, id={me.id})")
    print(f"📁 Файл сессии: {session_name}.session")
    print("   main.py подхватит его автоматически.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())