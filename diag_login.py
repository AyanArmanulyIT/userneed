"""Диагностика логина Telethon — что именно отвечает Telegram."""
import asyncio
import logging

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
)

from config import load_config

logging.basicConfig(level=logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)


async def main() -> None:
    cfg = load_config()

    # НЕ тот же файл, что у бота — отдельный, чтобы не сломать рабочие сессии.
    client = TelegramClient("diag_session", cfg.api_id, cfg.api_hash)
    await client.connect()
    print(f"✅ Подключился к Telegram (API_ID={cfg.api_id})\n")

    # Спрашиваем номер вручную — не через .env, чтобы ты видел, что вводишь.
    phone = input("Введи номер в формате +79991234567: ").strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    print(f"\n→ Отправляю запрос кода на {phone}...\n")

    try:
        sent = await client.send_code_request(phone)

        # Всё, что нужно знать, чтобы понять, куда ушёл код:
        print("✅ Telegram ПРИНЯЛ запрос кода.")
        print(f"  • Тип доставки:  {type(sent.type).__name__}")
        print(f"    (SentCodeTypeApp — в приложение Telegram)")
        print(f"    (SentCodeTypeSms — SMS на номер)")
        print(f"    (SentCodeTypeCall — голосовой звонок)")
        print(f"    (SentCodeTypeFlashCall — входящий звонок с номером кода)")
        print(f"  • Следующий код можно запросить через: {sent.timeout} сек")
        print(f"  • phone_code_hash: {sent.phone_code_hash[:24]}...")
        print()
        print("→ Проверь канал доставки и посмотри код:")
        print("   • если SentCodeTypeApp  — ОТКРОЙ TELEGRAM на телефоне (чат «Telegram»)")
        print("   • если SentCodeTypeSms  — ЖДИ SMS (1-3 мин)")
        print("   • если SentCodeTypeCall — ПРИМЕТ ВХОДЯЩИЙ ЗВОНОК (робот продиктует код)")

    except PhoneNumberInvalidError:
        print("❌ Неверный формат номера. Нужен формат +79991234567 без пробелов.")
    except PhoneNumberBannedError:
        print("🚫 Номер забанен в Telegram. Никогда не залогинится.")
    except PhoneNumberFloodError:
        print("⏳ Слишком много попыток. Telegram блокировал отправку.")
        print("   Жди 24 часа с этого IP или смени IP (VPN).")
    except FloodWaitError as exc:
        print(f"🚦 FloodWait: Telegram требует подождать {exc.seconds} сек.")
        print(f"   Это ~{exc.seconds // 60} минут.")
    except ApiIdInvalidError:
        print("❌ API_ID / API_HASH неверные. Проверь .env и my.telegram.org.")
    except Exception as exc:
        print(f"💥 Неожиданная ошибка: {type(exc).__name__}: {exc}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())