# UserNeed — Telegram Username Finder

## Что это

Telegram-бот для подбора свободных username в Telegram. Работает через
MTProto (Telethon), без скрапинга, с предварительным HTTP-отсевом
через t.me. Настроен на закрытую бету с белым списком.

Бот: @userneed_bot
Канал: @userneed

## Стек

- **aiogram 3.x** — интерфейс бота (кнопки, FSM, хендлеры)
- **Telethon** — MTProto-запросы (проверка занятости username)
- **aiohttp** — HTTP pre-check через t.me + парсинг Fragment
- **SQLite** — четыре базы: found, watchlist, fsm, feedback
- **asyncio** — всё в одном event loop
- Python 3.11+

## Архитектура

### Модули (`bot/`)
- `branding.py` — ВСЕ тексты бота. Никогда не хардкодить строки в handlers.py
- `keyboards.py` — inline-клавиатуры
- `handlers.py` — хендлеры, FSM, семафор генерации, middleware
- `utils.py` — chunk_lines, esc, progress_bar

### Сервисы (`services/`)
- `checker.py` — пул Telethon-клиентов с Round-Robin, HTTP pre-check, антифлуд
- `generator.py` — генерация имён (слоговые, словарные, брендовые, скрамбл), скоринг 1-10, поиск «похожих»
- `storage.py` — SQLite-база находок (found.db)
- `watchlist.py` — фоновый монитор избранных имён (watchlist.db)
- `sqlite_fsm.py` — FSM storage на SQLite (fsm.db) — состояния переживают рестарт
- `feedback.py` — обратная связь: user → админ → reply обратно (feedback.db)
- `session_guard.py` — сторож Telethon-сессий, алертит админов при падении
- `daily_report.py` — суточная сводка админам
- `backup_manager.py` — суточные бэкапы (found.db, watchlist.db, fsm.db, *.session, .env, assets/)

### Точка входа
- `main.py` — поднимает пул Telethon, чекер, aiogram, фоновые задачи
- `config.py` — читает .env, валидирует типы
- `qr_login.py` — разовый QR-логин для Telethon-сессий

## Ключевые решения (НЕ МЕНЯТЬ без обсуждения)

1. **HTML-разметка.** Все тексты — ParseMode.HTML. Символы `<`, `>`, `&`
   в тексте ОБЯЗАТЕЛЬНО экранировать (`&lt;`, `&gt;`, `&amp;`) или
   заменять словами («меньше 1 дня» вместо «< 1 дня»). Иначе TelegramBadRequest.

2. **Round-Robin между сессиями.** Запросы распределяются между
   Telethon-аккаунтами (acc1, acc2). Пауза 1.5-3 сек — ПЕР-СЕССИЯ, не глобальная.

3. **HTTP pre-check РАНЬШЕ MTProto.** Перед ResolveUsername идёт запрос
   на https://t.me/<name>. 90% занятых имён отсеиваются без MTProto.
   Экономит лимиты Telegram на порядок.

4. **Точный ответ: free / fragment / taken.** ResolveUsername +
   CheckUsername. `fragment` = имя есть, но только за TON на Fragment —
   бесплатно не занять.

5. **Закрытая бета.** BETA_MODE=1 + ALLOWED_USER_IDS. Вне списка — 
   AccessMiddleware отсекает на входе.

6. **Глобальный семафор генерации (GLOBAL_GEN_CONCURRENCY=1).** Два
   пользователя не могут запускать генерацию одновременно — иначе
   пул аккаунтов уходит в FloodWait.

7. **Жёсткий таймаут генерации = 900 сек.** Если прогон завис — 
   семафор освобождается принудительно, лимит пользователя не списывается.

8. **Не списывать лимит при отказе в очереди.** Если система занята — 
   пользователь получает отказ, но _try_consume не вызывается.

## Что НЕ делать

- ❌ Не добавлять autozahvat username на аккаунт пользователя — 
  нарушение Telegram ToS, гарантированный бан.
- ❌ Не использовать Redis — проект работает на слабом железе.
- ❌ Не хардкодить тексты в handlers.py — все строки в branding.py.
- ❌ Не смешивать MemoryStorage и SQLiteStorage — используется SQLiteStorage.
- ❌ Не делать Fragment аукцион — юридически опасно и технически 
  невозможно (эскроу usernames не существует вне официальной площадки).

## Стиль кода

- Type hints везде, где возможно
- `from __future__ import annotations` в каждом модуле
- Логирование через `logging.getLogger(__name__)`, не print
- Комментарии на русском
- Обработка ошибок: конкретные exceptions, не `except Exception` без причины
- Имена функций и переменных — на английском

## Команды для разработки

```bash
# Установка зависимостей
pip install -r requirements.txt

# Проверка конфига
python -m config

# Запуск бота
python main.py

# Просмотр логов
Get-Content logs\userneed.log -Tail 100 -Wait

# Сброс кэша
Get-ChildItem -Recurse -Filter "__pycache__" -Directory | Remove-Item -Recurse -Force

# Бэкап вручную
# В боте: /backup_now   