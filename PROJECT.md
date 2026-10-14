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
- **SQLite** — шесть баз: found, watchlist, fsm, feedback, quota, check_cache
- **asyncio** — всё в одном event loop
- Python 3.11+

## Архитектура

### Модули (`bot/`)
- `branding.py` — ВСЕ тексты бота. Никогда не хардкодить строки в handlers.py.
  Сам экранирует пользовательский ввод (`esc`).
- `keyboards.py` — inline-клавиатуры. `main_menu(is_admin)` — мониторинг только операторам.
- `handlers.py` — хендлеры, FSM, общая обвязка прогона `_start_run`
  (лимит → один прогон на пользователя → семафор → таймаут), middleware
- `utils.py` — chunk_lines, esc, progress_bar

### Сервисы (`services/`)
- `checker.py` — пул Telethon-клиентов с Round-Robin, HTTP pre-check, антифлуд,
  кэш результатов; публичные `seconds_until_unpark()`, `sessions_snapshot()`, `stats`
- `generator.py` — генерация имён (слоговые, словарные, брендовые, скрамбл),
  скоринг 1-10, поиск «похожих» (детерминированный, `[]` когда варианты кончились)
- `storage.py` — SQLite-база находок (found.db); `mark_claimed` — с привязкой к user_id
- `quota.py` — квота сеансов на пользователя (quota.db), переживает рестарт
- `check_cache.py` — кэш результатов проверки с TTL (check_cache.db)
- `watchlist.py` — фоновый монитор избранных имён (watchlist.db), только операторы
- `sqlite_fsm.py` — FSM storage на SQLite (fsm.db) — состояния переживают рестарт
- `feedback.py` — обратная связь: user → админ → reply обратно (feedback.db), сквозной ticket_id
- `session_guard.py` — сторож Telethon-сессий: сначала реконнект, потом один алерт
- `daily_report.py` — суточная сводка админам
- `backup_manager.py` — суточные бэкапы (*.db, assets/). `.env` и `*.session` —
  ТОЛЬКО при `BACKUP_INCLUDE_SECRETS=1`, архив не шифруется

### Точка входа
- `main.py` — поднимает пул Telethon, чекер, aiogram, фоновые задачи
- `config.py` — читает .env, валидирует типы, `runtime_warnings()`; `DATA_DIR` —
  где лежат базы, сессии, логи и бэкапы (по умолчанию — корень проекта)
- `qr_login.py` — разовый QR-логин для Telethon-сессий

## Ключевые решения (НЕ МЕНЯТЬ без обсуждения)

1. **HTML-разметка.** Все тексты — ParseMode.HTML. Символы `<`, `>`, `&`
   в тексте ОБЯЗАТЕЛЬНО экранировать (`&lt;`, `&gt;`, `&amp;`) или
   заменять словами («меньше 1 дня» вместо «< 1 дня»). Иначе TelegramBadRequest.
   Превью ссылок отключено глобально через `DefaultBotProperties`.

2. **Round-Robin между сессиями.** Запросы распределяются между
   Telethon-аккаунтами (acc1, acc2). Пауза 1.5-3 сек — ПЕР-СЕССИЯ, не глобальная.

3. **HTTP pre-check РАНЬШЕ MTProto.** Перед ResolveUsername идёт запрос
   на https://t.me/<name>. 90% занятых имён отсеиваются без MTProto.
   Экономит лимиты Telegram на порядок.

4. **Точный ответ: free / fragment / taken.** ResolveUsername +
   CheckUsername. `fragment` = имя есть, но только за TON на Fragment —
   бесплатно не занять.

5. **Закрытая бета.** BETA_MODE=1 + ALLOWED_USER_IDS. Вне списка —
   AccessMiddleware (OUTER middleware) отсекает до фильтров и хендлеров.

6. **Глобальный семафор генерации (GLOBAL_GEN_CONCURRENCY=1).** Два
   пользователя не могут запускать генерацию одновременно — иначе
   пул аккаунтов уходит в FloodWait. Пакетная проверка идёт через тот же семафор.

7. **Жёсткий таймаут прогона = 900 сек.** Если прогон завис —
   семафор освобождается принудительно, лимит пользователя не списывается.
   За 60 сек до него срабатывает мягкий лимит: прогон сворачивается сам,
   показывает частичный результат и завершается штатно (как «Прервать»).

8. **Квота списывается ПОСЛЕ работы, а не до.** `run.commit()` вызывается,
   когда прогон завершился штатно (в т.ч. по кнопке «Прервать»). Отказ в
   очереди, флуд всех сессий, таймаут, внутренняя ошибка, ноль проверок —
   сеанс остаётся у пользователя. Частичный результат при этом показывается.

9. **Один прогон на пользователя.** Второй запуск, пока первый идёт, —
   отказ без списания. Остановка — per-run `asyncio.Event`, не глобальный флаг.

## Что НЕ делать

- ❌ Не добавлять autozahvat username на аккаунт пользователя —
  нарушение Telegram ToS, гарантированный бан.
- ❌ Не использовать Redis — проект работает на слабом железе.
- ❌ Не хардкодить тексты в handlers.py — все строки в branding.py.
- ❌ Не смешивать MemoryStorage и SQLiteStorage — используется SQLiteStorage.
- ❌ Не делать Fragment аукцион — юридически опасно и технически
  невозможно (эскроу usernames не существует вне официальной площадки).
- ❌ Не класть `.env` и `*.session` в бэкапы по умолчанию — zip не шифруется.
- ❌ Не хранить квоту в памяти процесса — только SQLite.

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

# Проверка конфига и предупреждений
python -m config

# Статический анализ
python -m pyflakes .

# Запуск бота
python main.py

# Просмотр логов (Windows PowerShell)
Get-Content logs\userneed.log -Tail 100 -Wait

# Сброс кэша байткода (Windows PowerShell)
Get-ChildItem -Recurse -Filter "__pycache__" -Directory | Remove-Item -Recurse -Force

# Бэкап вручную
# В боте: /backup_now
```
