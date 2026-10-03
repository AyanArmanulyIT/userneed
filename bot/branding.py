"""
Тексты бота. Единый регламент формулировок.

Стиль: деловое обращение на «вы», сухие формулировки, служебные маркеры.
Все строки — HTML (aiogram ParseMode.HTML).

ВАЖНО: символы <, >, & в текстовых строках нужно писать как &lt;, &gt;, &amp;
иначе Telegram отвечает TelegramBadRequest «can't parse entities».
"""
from __future__ import annotations

BOT_NAME = "UserNeed"

_LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_DOT = "▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒"


def welcome(remaining: int, limit: int, is_admin: bool, first_name: str) -> str:
    """Экран /start."""
    if is_admin:
        quota = "Статус: <b>оператор</b> · <i>без ограничений</i>"
    else:
        quota = f"Лимит сеансов: <b>{max(0, remaining)}</b> / {limit}"

    name = first_name.strip() or "пользователь"

    return (
        f"<b>USERNEED</b>  ·  информационная система\n"
        f"{_LINE}\n\n"
        f"Здравствуйте, <b>{name}</b>.\n\n"
        f"Система производит подбор незанятых идентификаторов "
        f"(<code>username</code>) в Telegram. Проверка выполняется "
        f"по защищённому протоколу MTProto.\n\n"
        f"<b>Доступные модули:</b>\n"
        f"{_DOT}\n"
        f"<b>01</b>  ·  Генерация имён\n"
        f"        Автоматический подбор вариантов с оценкой\n"
        f"<b>02</b>  ·  Пакетная проверка\n"
        f"        Оценка занятости по загружаемому списку\n"
        f"<b>03</b>  ·  Мониторинг\n"
        f"        Фоновое отслеживание избранных имён\n"
        f"<b>04</b>  ·  История находок\n"
        f"        Просмотр и экспорт результатов\n"
        f"<b>05</b>  ·  Регламент\n"
        f"        Описание процедур и лимитов\n"
        f"<b>06</b>  ·  Связаться с админом\n"
        f"        Вопросы, идеи, баги\n"
        f"{_DOT}\n\n"
        f"{quota}\n\n"
        f"<i>Выберите модуль для начала работы.</i>"
    )


# =============================================================================
#                          Модуль 01 · Генерация
# =============================================================================

def gen_mode_intro(user_id: int, rem: int, limit: int, is_new: bool = False) -> str:
    """
    Экран выбора типа генерации.
    is_new=True — подсказка для первого запуска.
    """
    if rem < 0:
        hint = "Статус: <i>оператор · без ограничений</i>"
    else:
        hint = f"Лимит сеансов: <b>{rem}</b> / {limit}"

    onboarding = ""
    if is_new:
        onboarding = (
            f"\n\n<b>Совет для первого запуска:</b>\n"
            f"Начните с «02 · Случайные сочетания» — там свободных имён "
            f"в 10-20 раз больше, чем в осмысленных. Первые результаты "
            f"увидите за минуту."
        )

    return (
        f"<b>МОДУЛЬ 01 · ГЕНЕРАЦИЯ</b>\n"
        f"{_DOT}\n"
        f"Выберите тип идентификаторов:\n\n"
        f"<b>01</b>  ·  Осмысленные имена\n"
        f"        Слоговые, словарные, брендовые, служебные.\n"
        f"        Свободно ~1-3 %.\n\n"
        f"<b>02</b>  ·  Случайные сочетания\n"
        f"        Буквенный скрэмбл вида <code>kxvzq</code>.\n"
        f"        Свободно <b>40-70 %</b>.\n\n"
        f"<b>03</b>  ·  Похожие на ...\n"
        f"        Введите имя — система подберёт близкие варианты.\n\n"
        f"{hint}"
        f"{onboarding}"
    )


def gen_count_intro(mode: str, rem: int, limit: int) -> str:
    titles = {
        "smart": "осмысленных имён",
        "random": "случайных сочетаний",
    }
    title = titles.get(mode, mode)
    hint = (
        "Статус: <i>оператор · без ограничений</i>"
        if rem < 0
        else f"Лимит сеансов: <b>{rem}</b> / {limit}"
    )
    return (
        f"<b>МОДУЛЬ 01 · ГЕНЕРАЦИЯ</b>\n"
        f"{_DOT}\n"
        f"Режим: <b>{title}</b>\n\n"
        f"Укажите целевой объём подбора.\n\n"
        f"{hint}"
    )


def gen_similar_intro(rem: int, limit: int) -> str:
    hint = (
        "Статус: <i>оператор · без ограничений</i>"
        if rem < 0
        else f"Лимит сеансов: <b>{rem}</b> / {limit}"
    )
    return (
        f"<b>МОДУЛЬ 01 · ПОХОЖИЕ ИМЕНА</b>\n"
        f"{_DOT}\n"
        f"Введите базовое имя — система сгенерирует фонетически\n"
        f"близкие варианты.\n\n"
        f"<b>Примеры:</b>\n"
        f"  <code>vital</code>  →  vitoz, vitaly, vitax\n"
        f"  <code>alex</code>   →  alexx, alexz, elex\n"
        f"  <code>space</code>  →  spaci, spaze, spize\n\n"
        f"{hint}"
    )


def gen_similar_pick_count(base: str, candidates: int, rem: int, limit: int) -> str:
    hint = (
        "Статус: <i>оператор · без ограничений</i>"
        if rem < 0
        else f"Лимит сеансов: <b>{rem}</b> / {limit}"
    )
    return (
        f"<b>МОДУЛЬ 01 · ПОХОЖИЕ НА «{base}»</b>\n"
        f"{_DOT}\n"
        f"Базовое имя: <code>{base}</code>\n"
        f"Сгенерировано вариантов: <b>{candidates}</b>\n\n"
        f"Укажите, сколько проверить.\n\n"
        f"{hint}"
    )


def search_started(target: int, base: str | None = None) -> str:
    extra = (
        f"\n<i>Режим: поиск похожих на «{base}».</i>"
        if base
        else ""
    )
    return (
        f"<b>МОДУЛЬ 01 · ОБРАБОТКА</b>\n"
        f"{_DOT}\n"
        f"Целевой объём: <b>{target}</b> позиций."
        f"{extra}\n\n"
        f"<i>Проверка выполняется пакетами. Остановка доступна в любой момент.</i>"
    )


def search_progress(target: int, found: list[tuple[str, int, str]], tested: int) -> str:
    lines = [
        f"<b>МОДУЛЬ 01 · ГЕНЕРАЦИЯ</b>",
        f"{_DOT}",
        f"Цель:              <b>{target}</b>",
        f"Найдено:           <b>{len(found)}</b>",
        f"Проверено позиций: <b>{tested}</b>",
    ]
    if found:
        lines.append("")
        lines.append("<b>ПОДОБРАННЫЕ ИДЕНТИФИКАТОРЫ:</b>")
        for i, (name, sc, _) in enumerate(found[:10], 1):
            lines.append(f"  {i:>2}.  <code>{name}</code>   ·  {sc}/10")
        if len(found) > 10:
            lines.append(f"  <i>…и ещё {len(found) - 10}</i>")
    return "\n".join(lines)


def search_final(
    found: list[tuple[str, int, str]],
    tested: int,
    stopped: bool,
    remaining_line: str,
) -> list[str]:
    header = (
        f"<b>МОДУЛЬ 01 · ОБРАБОТКА ПРЕРВАНА</b>"
        if stopped
        else f"<b>МОДУЛЬ 01 · ОБРАБОТКА ЗАВЕРШЕНА</b>"
    )
    lines = [
        header,
        _LINE,
        f"Проверено позиций:     <b>{tested}</b>",
        f"Свободных:             <b>{len(found)}</b>",
        "",
        "<b>РЕЗУЛЬТАТ:</b>",
        "",
    ]
    for i, (name, sc, why) in enumerate(found, 1):
        lines.append(
            f"  {i:>2}.  <code>{name}</code>   ·   <b>{sc}/10</b>   ·   <i>{why}</i>"
        )
    lines.append("")
    lines.append("<i>Ниже — кнопки действий. «Занял» уберёт имя из")
    lines.append("рекомендаций, «Похожие» найдёт варианты.</i>")
    lines.append("")
    lines.append(remaining_line)
    return lines


# =============================================================================
#                        Модуль 02 · Пакетная проверка
# =============================================================================

def bulk_intro(rem: int, limit: int, max_items: int) -> str:
    hint = (
        "Статус: <i>оператор · без ограничений</i>"
        if rem < 0
        else f"Лимит сеансов: <b>{rem}</b> / {limit}"
    )
    return (
        f"<b>МОДУЛЬ 02 · ПАКЕТНАЯ ПРОВЕРКА</b>\n"
        f"{_DOT}\n"
        f"Направьте список идентификаторов:\n"
        f"    ·  по одному значению в строке;\n"
        f"    ·  префикс <code>@</code> — опционально;\n"
        f"    ·  до <b>{max_items}</b> позиций.\n\n"
        f"{hint}"
    )


def bulk_report(
    free: list[str],
    fragment: list[str],
    taken: list[str],
    invalid: list[str],
    errors: list[str],
    remaining_line: str,
) -> list[str]:
    lines = [f"<b>МОДУЛЬ 02 · ОБРАБОТКА ЗАВЕРШЕНА</b>", _LINE, ""]

    if free:
        lines.append(f"<b>СВОБОДНЫЕ · можно занять · {len(free)}</b>")
        for n in free:
            lines.append(f"  ✓  <code>{n}</code>")
        lines.append("")

    if fragment:
        lines.append(f"<b>ТОЛЬКО ЗА TON (Fragment) · {len(fragment)}</b>")
        for n in fragment:
            lines.append(f"  $  <code>{n}</code>")
        lines.append("")

    if taken:
        lines.append(f"<b>ЗАНЯТЫЕ · {len(taken)}</b>")
        for n in taken:
            lines.append(f"  ✗  <code>{n}</code>")
        lines.append("")

    if invalid:
        lines.append(f"<b>НЕКОРРЕКТНЫЙ ФОРМАТ · {len(invalid)}</b>")
        for n in invalid:
            lines.append(f"  ⚠  <code>{n}</code>")
        lines.append("")

    if errors:
        lines.append(f"<b>ОШИБКА ПРОВЕРКИ · {len(errors)}</b>")
        for n in errors:
            lines.append(f"  ⚠  <code>{n}</code>")
        lines.append("")

    if not any([free, fragment, taken, invalid, errors]):
        lines.append("<i>Список пуст.</i>")

    lines.append(remaining_line)
    return lines


# =============================================================================
#                        Модуль 03 · Watchlist
# =============================================================================

def watch_intro(total: int, breakdown: dict[str, int]) -> str:
    return (
        f"<b>МОДУЛЬ 03 · МОНИТОРИНГ</b>\n"
        f"{_DOT}\n"
        f"Позиций в списке: <b>{total}</b>\n"
        f"    свежие: <b>{breakdown['hot']}</b>,\n"
        f"    тёплые: <b>{breakdown['warm']}</b>,\n"
        f"    холодные: <b>{breakdown['cold']}</b>,\n"
        f"    архив: <b>{breakdown['sleeping']}</b>\n\n"
        f"Система проверяет позиции в фоне и присылает сигнал\n"
        f"при освобождении.\n\n"
        f"<i>Добавление: одно имя в строке.</i>"
    )


def watch_list_header(total: int) -> str:
    return (
        f"<b>МОДУЛЬ 03 · СПИСОК МОНИТОРИНГА</b>\n"
        f"{_DOT}\n"
        f"Всего позиций: <b>{total}</b>\n"
    )


STATUS_MARKS = {
    "free": "✓",
    "fragment": "$",
    "taken": "✗",
    "invalid": "⚠",
    "error": "⚠",
    "pending": "…",
}


# =============================================================================
#                        Модуль 04 · История
# =============================================================================

def found_intro(stats: dict[str, int]) -> str:
    return (
        f"<b>МОДУЛЬ 04 · ИСТОРИЯ НАХОДОК</b>\n"
        f"{_DOT}\n"
        f"Всего записей: <b>{stats.get('total', 0)}</b>\n"
        f"Свободных в базе: <b>{stats.get('free', 0)}</b>\n"
        f"Отмечено как занято: <b>{stats.get('claimed', 0)}</b>\n\n"
        f"Доступные действия:\n"
        f"    ·  просмотр последних находок;\n"
        f"    ·  фильтр по периоду;\n"
        f"    ·  экспорт в CSV.\n"
    )


def found_list_header(count: int, period: str) -> str:
    return (
        f"<b>ИСТОРИЯ · {period}</b>\n"
        f"{_DOT}\n"
        f"Найдено записей: <b>{count}</b>\n"
    )


# =============================================================================
#                          Общие сообщения
# =============================================================================

def limit_reached(limit: int) -> str:
    return (
        f"<b>ДОСТУП ОГРАНИЧЕН</b>\n"
        f"{_DOT}\n"
        f"Установленный лимит сеансов исчерпан: "
        f"<b>{limit}</b> из <b>{limit}</b>.\n\n"
        f"Обратитесь к администратору для расширения квоты."
    )


def flooded_pause(seconds: int) -> str:
    minutes = seconds // 60
    eta = f"{minutes} мин." if minutes > 0 else f"{seconds} сек."
    return (
        f"<b>ВРЕМЕННАЯ ПРИОСТАНОВКА ОБСЛУЖИВАНИЯ</b>\n"
        f"{_DOT}\n"
        f"Все активные каналы проверки временно недоступны.\n\n"
        f"Ожидаемое восстановление: <b>{eta}</b>.\n\n"
        f"<i>Повторная попытка возможна после завершения периода ожидания.</i>"
    )


def remaining_line(remaining: int, limit: int, is_admin: bool) -> str:
    if is_admin:
        return "Статус: <i>оператор · без ограничений</i>"
    return f"Лимит сеансов: <b>{max(0, remaining)}</b> / {limit}"


def error_generic(reason: str) -> str:
    return (
        f"<b>ОБРАБОТКА ПРЕРВАНА</b>\n"
        f"{_DOT}\n"
        f"<i>{reason}</i>\n\n"
        f"Вернитесь в панель управления и повторите операцию."
    )


def claim_ack_text(username: str) -> str:
    """Подтверждение, когда пользователь нажал «✓ Занял»."""
    return (
        f"<b>ОТМЕЧЕНО</b>\n"
        f"{_DOT}\n"
        f"<code>@{username}</code> — твой.\n\n"
        f"<i>Имя убрано из рекомендаций. Оно не появится в следующих "
        f"генерациях и не будет отслеживаться в мониторинге.</i>"
    )


# =============================================================================
#                            Закрытая бета
# =============================================================================

NO_ACCESS_TEXT = (
    f"<b>ДОСТУП ОГРАНИЧЕН</b>\n"
    f"{_DOT}\n"
    f"Система работает в режиме <b>закрытого тестирования</b>.\n"
    f"Количество одновременных пользователей ограничено — "
    f"это защита от блокировок со стороны Telegram.\n\n"
    f"Для получения доступа:\n"
    f"  ·  напишите администратору в личку;\n"
    f"  ·  укажите свой Telegram ID (узнать: <code>@userinfobot</code>);\n"
    f"  ·  дождитесь подтверждения.\n\n"
    f"<i>Открытый доступ будет запущен после стабилизации нагрузки.</i>"
)


def queue_wait_text(wait_hint: int) -> str:
    return (
        f"<b>СИСТЕМА ЗАГРУЖЕНА</b>\n"
        f"{_DOT}\n"
        f"В данный момент выполняется обработка запроса другого "
        f"пользователя. Система допускает одну генерацию одновременно — "
        f"это защищает каналы проверки от блокировок Telegram.\n\n"
        f"Повторите попытку через <b>{wait_hint} сек.</b>\n\n"
        f"<i>Спасибо за понимание.</i>"
    )   

# =============================================================================
#                            Обратная связь
# =============================================================================

def feedback_intro(max_per_hour: int) -> str:
    return (
        f"<b>СВЯЗЬ С АДМИНИСТРАЦИЕЙ</b>\n"
        f"{_DOT}\n"
        f"Напишите текст — он уйдёт напрямую администраторам.\n"
        f"Ответ придёт сюда же, в этот чат.\n\n"
        f"<b>Что можно написать:</b>\n"
        f"  ·  сообщить о проблеме или ошибке;\n"
        f"  ·  предложить улучшение;\n"
        f"  ·  задать вопрос по работе системы.\n\n"
        f"<b>Ограничения:</b>\n"
        f"  ·  длина: 5-1000 символов;\n"
        f"  ·  не больше <b>{max_per_hour}</b> обращений в час.\n\n"
        f"<i>Введите текст следующим сообщением или нажмите «Отмена».</i>"
    )


def feedback_thanks(ticket_id: int) -> str:
    return (
        f"<b>ОБРАЩЕНИЕ ПРИНЯТО</b>\n"
        f"{_DOT}\n"
        f"Номер в очереди: <code>#{ticket_id}</code>\n\n"
        f"Администратор ответит в этот же чат. Обычно — в течение суток.\n\n"
        f"<i>Спасибо, что помогаете проекту.</i>"
    )


def feedback_rate_limited(minutes: int) -> str:
    return (
        f"<b>СЛИШКОМ ЧАСТО</b>\n"
        f"{_DOT}\n"
        f"Лимит обращений — не больше 3 в час.\n\n"
        f"Следующее можно отправить через <b>{minutes} мин.</b>"
    )


def feedback_invalid_length() -> str:
    return (
        f"<b>НЕКОРРЕКТНАЯ ДЛИНА</b>\n"
        f"{_DOT}\n"
        f"Текст должен быть от 5 до 1000 символов. Пришлите ещё раз."
    )


def admin_feedback_header(
    ticket_id: int, user_id: int, username: str, first_name: str
) -> str:
    """Заголовок, который видит админ в личке."""
    uname = f"@{username}" if username else "без username"
    name = first_name or "—"
    return (
        f"<b>ОБРАЩЕНИЕ #{ticket_id}</b>\n"
        f"{_LINE}\n\n"
        f"<b>От:</b> {name}\n"
        f"<b>Кто:</b> <code>{uname}</code>\n"
        f"<b>ID:</b> <code>{user_id}</code>\n\n"
        f"<b>Сообщение:</b>\n"
    )


def admin_feedback_footer() -> str:
    return (
        f"\n\n<i>Чтобы ответить — сделайте reply на это сообщение.\n"
        f"Ответ уйдёт пользователю.</i>"
    )               