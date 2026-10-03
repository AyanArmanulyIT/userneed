# 🎯 UserNeed — Telegram Username Finder

Бот ищет свободные короткие и красивые username'ы в Telegram
через MTProto (без скрапинга и капчи).

## Возможности

- 🎲 **Генератор** — слоговые (`vitoz`), брендовые (`zenix`), статусные (`v_max`).
- 📋 **Массовая проверка** — до 200 имён за раз, живой прогресс.
- ⚡ **HTTP pre-check** через `t.me` — 90% занятых имён отсеиваются без MTProto.
- 🔁 **Round-Robin** между несколькими аккаунтами.
- 🚦 **Анти-флуд** — FloodWait паркует одну сессию, работает другая.
- 🎟 **Лимит** 5 бесплатных запусков (админы — безлимит).

## Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# заполни .env — BOT_TOKEN, API_ID, API_HASH, ADMIN_IDS
python main.py