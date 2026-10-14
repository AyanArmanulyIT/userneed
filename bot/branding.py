"""
Тексты бота. Единый регламент формулировок.

Стиль: деловое обращение на «вы», сухие формулировки, служебные маркеры.
Все строки — HTML (aiogram ParseMode.HTML).

Правила модуля:
  • ВСЕ пользовательские тексты бота живут здесь. В handlers.py строк нет.
  • Символы <, >, & в статическом тексте пишутся как &lt;, &gt;, &amp;.
  • Всё, что пришло от пользователя (имя, username, текст обращения),
    функции экранируют САМИ через esc() — вызывающий код передаёт сырые
    значения. Исключение — параметры с суффиксом _html: они уже готовы.
  • Функции возвращают либо str (одно сообщение), либо list[str]
    (строки, которые потом режутся chunk_lines на сообщения ≤ 4096).
"""
from __future__ import annotations

from bot.utils import esc

BOT_NAME = "UserNeed"

LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
DOT = "▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒"

# Обратная совместимость со старым кодом, который обращался к «приватным»
# именам. Новый код использует LINE / DOT.
_LINE = LINE
_DOT = DOT


# =============================================================================
#                          Вспомогательные
# =============================================================================

def _quota_hint(rem: int, limit: int) -> str:
    """Строка о лимите. rem < 0 означает «оператор, без ограничений»."""
    if rem < 0:
        return "Статус: <i>оператор · без ограничений</i>"
    return f"Лимит сеансов: <b>{max(0, rem)}</b> / {limit}"


def remaining_line(remaining: int, limit: int, is_admin: bool) -> str:
    if is_admin:
        return _quota_hint(-1, limit)
    return _quota_hint(remaining, limit)


def _eta(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    return f"{minutes} мин." if minutes > 0 else f"{seconds} сек."


# =============================================================================
#                               Экран /start
# =============================================================================

def welcome(
    remaining: int,
    limit: int,
    is_admin: bool,
    first_name: str,
    show_watch: bool = True,
) -> str:
    """Экран /start. show_watch=False — скрыть модуль 03 (он только для операторов)."""
    quota = remaining_line(remaining, limit, is_admin)
    name = esc(first_name.strip()) or "пользователь"

    modules = [
        "<b>01</b>  ·  Генерация имён",
        "        Автоматический подбор вариантов с оценкой",
        "<b>02</b>  ·  Пакетная проверка",
        "        Оценка занятости по загружаемому списку",
    ]
    if show_watch:
        modules += [
            "<b>03</b>  ·  Мониторинг",
            "        Фоновое отслеживание избранных имён",
        ]
    modules += [
        "<b>04</b>  ·  История находок",
        "        Просмотр и экспорт результатов",
        "<b>05</b>  ·  Регламент",
        "        Описание процедур и лимитов",
        "<b>06</b>  ·  Связаться с админом",
        "        Вопросы, идеи, ошибки",
    ]

    return (
        f"<b>USERNEED</b>  ·  информационная система\n"
        f"{LINE}\n\n"
        f"Здравствуйте, <b>{name}</b>.\n\n"
        f"Система производит подбор незанятых идентификаторов "
        f"(<code>username</code>) в Telegram. Проверка выполняется "
        f"по протоколу MTProto.\n\n"
        f"<b>Доступные модули:</b>\n"
        f"{DOT}\n"
        + "\n".join(modules)
        + f"\n{DOT}\n\n"
        f"{quota}\n\n"
        f"<i>Выберите модуль для начала работы.</i>"
    )


CANCELLED_TEXT = (
    "<b>ОТМЕНЕНО</b>\n"
    f"{DOT}\n"
    "Текущий ввод сброшен. Вернитесь в панель управления."
)

FALLBACK_TEXT = (
    "Команда не распознана.\n\n"
    "·  /start — панель управления\n"
    "·  /help — регламент\n"
    "·  /cancel — сбросить текущий ввод"
)

MESSAGE_UNAVAILABLE = "Сообщение устарело. Отправьте /start."


# =============================================================================
#                          Модуль 05 · Регламент
# =============================================================================

def help_text(limit: int, bulk_limit: int, timeout_minutes: int) -> str:
    return (
        f"<b>МОДУЛЬ 05 · РЕГЛАМЕНТ</b>\n"
        f"{DOT}\n"
        f"<b>Назначение.</b> Система подбирает незанятые <code>username</code> "
        f"в Telegram и проверяет их по протоколу MTProto через пул служебных "
        f"аккаунтов. Автоматическая регистрация имён не производится — "
        f"имя вы занимаете вручную в настройках Telegram.\n\n"
        f"<b>Статусы проверки:</b>\n"
        f"  ✓  свободно — можно занять прямо сейчас;\n"
        f"  $  только за TON — имя продаётся на Fragment, бесплатно не занять;\n"
        f"  ✗  занято;\n"
        f"  ⚠  некорректный формат или ошибка проверки.\n\n"
        f"<b>Требования к имени:</b>\n"
        f"  ·  от 5 до 32 символов: латиница, цифры, «_»;\n"
        f"  ·  первый символ — буква, последний — буква или цифра;\n"
        f"  ·  без двойного подчёркивания.\n\n"
        f"<b>Лимиты:</b>\n"
        f"  ·  <b>{limit}</b> сеанса(ов) на участника — генерация или "
        f"пакетная проверка (до {bulk_limit} имён за раз);\n"
        f"  ·  сеанс списывается только после завершения обработки — "
        f"при перегрузке, сбое или таймауте не списывается;\n"
        f"  ·  одновременно выполняется одна обработка на всю систему — "
        f"это защита аккаунтов от блокировки;\n"
        f"  ·  максимальное время одной обработки — {timeout_minutes} мин.\n\n"
        f"<b>Команды:</b>\n"
        f"  /start — панель управления\n"
        f"  /help — этот регламент\n"
        f"  /found — история находок\n"
        f"  /cancel — сбросить текущий ввод\n\n"
        f"<i>Вопросы и предложения — через модуль 06.</i>"
    )


# Статический вариант для кода, которому неоткуда взять настройки.
HELP_TEXT = help_text(limit=3, bulk_limit=200, timeout_minutes=15)


# =============================================================================
#                            Закрытая бета
# =============================================================================

def no_access_text(user_id: int | None = None) -> str:
    id_line = (
        f"Ваш Telegram ID: <code>{user_id}</code> — укажите его администратору.\n\n"
        if user_id
        else "Узнать свой Telegram ID: <code>@userinfobot</code>.\n\n"
    )
    return (
        f"<b>ДОСТУП ОГРАНИЧЕН</b>\n"
        f"{DOT}\n"
        f"Система работает в режиме <b>закрытого тестирования</b>.\n"
        f"Количество одновременных пользователей ограничено — "
        f"это защита от блокировок со стороны Telegram.\n\n"
        f"{id_line}"
        f"<i>Открытый доступ будет запущен после стабилизации нагрузки.</i>"
    )


NO_ACCESS_TEXT = no_access_text()
NO_ACCESS_ALERT = "Доступ закрыт. Система в закрытой бете."

ADMIN_ONLY = "Операция доступна только операторам системы."
ADMIN_ONLY_ALERT = "Модуль доступен только операторам."


# =============================================================================
#                          Модуль 01 · Генерация
# =============================================================================

def gen_mode_intro(user_id: int, rem: int, limit: int, is_new: bool = False) -> str:
    """Экран выбора типа генерации. is_new=True — подсказка для первого запуска."""
    onboarding = ""
    if is_new:
        onboarding = (
            "\n\n<b>Совет для первого запуска:</b>\n"
            "Начните с «02 · Случайные сочетания» — там свободных имён "
            "в 10-20 раз больше, чем в осмысленных. Первые результаты "
            "увидите за минуту."
        )

    return (
        f"<b>МОДУЛЬ 01 · ГЕНЕРАЦИЯ</b>\n"
        f"{DOT}\n"
        f"Выберите тип идентификаторов:\n\n"
        f"<b>01</b>  ·  Осмысленные имена\n"
        f"        Слоговые, словарные, брендовые, служебные.\n"
        f"        Свободно ~1-3 %.\n\n"
        f"<b>02</b>  ·  Случайные сочетания\n"
        f"        Буквенный скрэмбл вида <code>kxvzq</code>.\n"
        f"        Свободно <b>40-70 %</b>.\n\n"
        f"<b>03</b>  ·  Похожие на ...\n"
        f"        Введите имя — система подберёт близкие варианты.\n\n"
        f"{_quota_hint(rem, limit)}"
        f"{onboarding}"
    )


def gen_count_intro(mode: str, rem: int, limit: int) -> str:
    titles = {
        "smart": "осмысленных имён",
        "random": "случайных сочетаний",
    }
    return (
        f"<b>МОДУЛЬ 01 · ГЕНЕРАЦИЯ</b>\n"
        f"{DOT}\n"
        f"Режим: <b>{titles.get(mode, esc(mode))}</b>\n\n"
        f"Укажите целевой объём подбора.\n\n"
        f"{_quota_hint(rem, limit)}"
    )


def gen_similar_intro(rem: int, limit: int) -> str:
    return (
        f"<b>МОДУЛЬ 01 · ПОХОЖИЕ ИМЕНА</b>\n"
        f"{DOT}\n"
        f"Введите базовое имя — система сгенерирует фонетически\n"
        f"близкие варианты.\n\n"
        f"<b>Примеры:</b>\n"
        f"  <code>vital</code>  →  vitoz, vitaly, vitax\n"
        f"  <code>alex</code>   →  alexx, alexz, elex\n"
        f"  <code>space</code>  →  spaci, spaze, spize\n\n"
        f"{_quota_hint(rem, limit)}"
    )


def similar_base_invalid(max_len: int) -> str:
    return (
        "Некорректное базовое имя. Нужны только латинские буквы, "
        f"от 3 до {max_len} символов."
    )


def similar_no_candidates(base: str) -> str:
    return (
        f"По базе <code>{esc(base)}</code> не удалось сгенерировать варианты. "
        f"Попробуйте другое имя."
    )


def gen_similar_pick_count(base: str, candidates: int, rem: int, limit: int) -> str:
    return (
        f"<b>МОДУЛЬ 01 · ПОХОЖИЕ НА «{esc(base)}»</b>\n"
        f"{DOT}\n"
        f"Базовое имя: <code>{esc(base)}</code>\n"
        f"Сгенерировано вариантов: <b>{candidates}</b>\n\n"
        f"Укажите, сколько проверить.\n\n"
        f"{_quota_hint(rem, limit)}"
    )


SIMILAR_PREPARING = "Готовлю похожие…"


def similar_quick_header(base: str) -> str:
    return f"<b>Похожие на «{esc(base)}»</b>\n<i>Готовлю выборку…</i>"


def search_started(target: int, base: str | None = None) -> str:
    extra = f"\n<i>Режим: поиск похожих на «{esc(base)}».</i>" if base else ""
    return (
        f"<b>МОДУЛЬ 01 · ОБРАБОТКА</b>\n"
        f"{DOT}\n"
        f"Целевой объём: <b>{target}</b> позиций."
        f"{extra}\n\n"
        f"<i>Проверка выполняется пакетами. Остановка доступна в любой момент.</i>"
    )


def search_progress(target: int, found: list[tuple[str, int, str]], tested: int) -> str:
    lines = [
        "<b>МОДУЛЬ 01 · ГЕНЕРАЦИЯ</b>",
        DOT,
        f"Цель:              <b>{target}</b>",
        f"Найдено:           <b>{len(found)}</b>",
        f"Проверено позиций: <b>{tested}</b>",
    ]
    if found:
        lines.append("")
        lines.append("<b>ПОДОБРАННЫЕ ИДЕНТИФИКАТОРЫ:</b>")
        for i, (name, sc, _) in enumerate(found[:10], 1):
            lines.append(f"  {i:>2}.  <code>{esc(name)}</code>   ·  {sc}/10")
        if len(found) > 10:
            lines.append(f"  <i>…и ещё {len(found) - 10}</i>")
    return "\n".join(lines)


def search_final(
    found: list[tuple[str, int, str]],
    tested: int,
    stopped: bool,
    remaining_line: str,
    note: str = "",
) -> list[str]:
    """
    Итоговый отчёт генерации.
    note — дополнительная строка (например, уведомление о флуде), уже HTML.
    """
    header = (
        "<b>МОДУЛЬ 01 · ОБРАБОТКА ПРЕРВАНА</b>"
        if stopped
        else "<b>МОДУЛЬ 01 · ОБРАБОТКА ЗАВЕРШЕНА</b>"
    )
    lines = [
        header,
        LINE,
        f"Проверено позиций:     <b>{tested}</b>",
        f"Свободных:             <b>{len(found)}</b>",
        "",
        "<b>РЕЗУЛЬТАТ:</b>",
        "",
    ]
    for i, (name, sc, why) in enumerate(found, 1):
        lines.append(
            f"  {i:>2}.  <code>{esc(name)}</code>   ·   <b>{sc}/10</b>   ·   <i>{esc(why)}</i>"
        )
    if note:
        lines.append("")
        lines.append(note)
    lines.append("")
    lines.append("<i>Ниже — кнопки действий. «Занял» уберёт имя из")
    lines.append("рекомендаций, «Похожие» найдёт варианты.</i>")
    lines.append("")
    lines.append(remaining_line)
    return lines


def gen_nothing_found(min_score: int, tested: int, remaining_line: str) -> str:
    """Прогон завершился штатно, но подходящих свободных имён не нашлось."""
    return (
        f"<b>МОДУЛЬ 01 · ОБРАБОТКА ЗАВЕРШЕНА</b>\n"
        f"{LINE}\n"
        f"Проверено позиций:     <b>{tested}</b>\n"
        f"Свободных:             <b>0</b>\n\n"
        f"<i>Позиций, удовлетворяющих фильтру (оценка не ниже {min_score}), "
        f"не обнаружено. Попробуйте другой режим или увеличьте объём.</i>\n\n"
        f"{remaining_line}"
    )


def gen_done_short(found: int, tested: int, stopped: bool) -> str:
    title = "Обработка прервана" if stopped else "Обработка завершена"
    return f"<b>{title}.</b>\nСвободно: <b>{found}</b>, проверено: {tested}."


def time_limit_note(minutes: int) -> str:
    """Пометка для отчёта, когда прогон свернулся по мягкому лимиту времени."""
    return (
        f"<i>Достигнут предел времени обработки ({minutes} мин) — "
        f"показан результат на момент остановки.</i>"
    )


def flood_note(seconds: int) -> str:
    """Короткая пометка для частичного отчёта."""
    return (
        f"<i>Каналы проверки временно исчерпаны — обработка остановлена "
        f"досрочно. Восстановление через ~{_eta(seconds)} "
        f"Сеанс не списан.</i>"
    )


GEN_TIMEOUT_REASON = (
    "Обработка превысила максимальное время. "
    "Система принудительно завершила сеанс. Сеанс не списан."
)
GEN_STOPPING = "Завершаю обработку…"
GEN_NO_ACTIVE_RUN = "Активной обработки нет."
RUN_ALREADY_ACTIVE = (
    "У вас уже выполняется обработка. Дождитесь её завершения "
    "или нажмите «Завершить»."
)

# Короткие ответы на некорректные callback-данные.
CB_BAD_REQUEST = "Некорректный запрос."
CB_BAD_COUNT = "Некорректный объём."
CB_BAD_MODE = "Некорректный режим."
CB_BAD_BASE = "Некорректное базовое имя."
CB_UNKNOWN_FILTER = "Неизвестный фильтр."


def count_out_of_range(max_allowed: int) -> str:
    return f"Допустимо от 1 до {max_allowed}."


def claim_ack_alert(username: str) -> str:
    return f"✓ @{username} отмечен как занятый"


CLAIM_NOT_YOURS = "Это имя не из вашего списка находок."


def claim_ack_text(username: str) -> str:
    """Подтверждение, когда пользователь нажал «✓ Занял»."""
    return (
        f"<b>ОТМЕЧЕНО</b>\n"
        f"{DOT}\n"
        f"<code>@{esc(username)}</code> — ваш.\n\n"
        f"<i>Имя убрано из рекомендаций: оно не появится в следующих "
        f"генерациях.</i>"
    )


# =============================================================================
#                        Модуль 02 · Пакетная проверка
# =============================================================================

def bulk_intro(rem: int, limit: int, max_items: int) -> str:
    return (
        f"<b>МОДУЛЬ 02 · ПАКЕТНАЯ ПРОВЕРКА</b>\n"
        f"{DOT}\n"
        f"Направьте список идентификаторов:\n"
        f"    ·  по одному значению в строке;\n"
        f"    ·  префикс <code>@</code> — опционально;\n"
        f"    ·  до <b>{max_items}</b> позиций.\n\n"
        f"{_quota_hint(rem, limit)}"
    )


BULK_EMPTY = "Список не содержит позиций."
BULK_TEXT_EXPECTED = "Ожидается текстовый список: одно значение в строке."


def bulk_too_many(count: int, limit: int) -> str:
    return f"Превышен объём: <b>{count}</b>. Максимум {limit}."


def bulk_progress(done: int, total: int, free: int, fragment: int, bar: str) -> str:
    pct = int(done * 100 / total) if total else 0
    return (
        f"<b>МОДУЛЬ 02 · ОБРАБОТКА</b>\n"
        f"Обработано: <b>{done}</b> / {total}\n"
        f"<code>{bar}</code>  {pct}%\n"
        f"Свободно: <b>{free}</b>  ·  только за TON: <b>{fragment}</b>"
    )


def bulk_done_short(free: int, total: int, stopped: bool = False) -> str:
    title = "Обработка прервана" if stopped else "Обработка завершена"
    return f"<b>{title}.</b>  Свободно: <b>{free}</b> из {total}."


def bulk_report(
    free: list[str],
    fragment: list[str],
    taken: list[str],
    invalid: list[str],
    errors: list[str],
    remaining_line: str,
    unchecked: list[str] | tuple[str, ...] = (),
    note: str = "",
) -> list[str]:
    """
    Отчёт пакетной проверки.
    unchecked — имена, до которых обработка не дошла (стоп или флуд).
    note — дополнительная HTML-строка перед подписью о лимите.
    """
    stopped = bool(unchecked)
    header = (
        "<b>МОДУЛЬ 02 · ОБРАБОТКА ПРЕРВАНА</b>"
        if stopped
        else "<b>МОДУЛЬ 02 · ОБРАБОТКА ЗАВЕРШЕНА</b>"
    )
    lines = [header, LINE, ""]

    def _section(title: str, mark: str, names: list[str] | tuple[str, ...]) -> None:
        if not names:
            return
        lines.append(f"<b>{title} · {len(names)}</b>")
        for n in names:
            lines.append(f"  {mark}  <code>{esc(n)}</code>")
        lines.append("")

    _section("СВОБОДНЫЕ · можно занять", "✓", free)
    _section("ТОЛЬКО ЗА TON (Fragment)", "$", fragment)
    _section("ЗАНЯТЫЕ", "✗", taken)
    _section("НЕКОРРЕКТНЫЙ ФОРМАТ", "⚠", invalid)
    _section("ОШИБКА ПРОВЕРКИ", "⚠", errors)
    _section("НЕ ПРОВЕРЕНО", "…", unchecked)

    if not any([free, fragment, taken, invalid, errors, unchecked]):
        lines.append("<i>Список пуст.</i>")
        lines.append("")

    if note:
        lines.append(note)
        lines.append("")
    lines.append(remaining_line)
    return lines


# =============================================================================
#                        Модуль 03 · Watchlist
# =============================================================================

def watch_intro(total: int, breakdown: dict[str, int]) -> str:
    return (
        f"<b>МОДУЛЬ 03 · МОНИТОРИНГ</b>\n"
        f"{DOT}\n"
        f"Позиций в списке: <b>{total}</b>\n"
        f"    свежие: <b>{breakdown.get('hot', 0)}</b>,\n"
        f"    тёплые: <b>{breakdown.get('warm', 0)}</b>,\n"
        f"    холодные: <b>{breakdown.get('cold', 0)}</b>,\n"
        f"    архив: <b>{breakdown.get('sleeping', 0)}</b>\n\n"
        f"Система проверяет позиции в фоне и присылает сигнал\n"
        f"при освобождении.\n\n"
        f"<i>Добавление: одно имя в строке.</i>"
    )


def watch_list_header(total: int) -> str:
    return (
        f"<b>МОДУЛЬ 03 · СПИСОК МОНИТОРИНГА</b>\n"
        f"{DOT}\n"
        f"Всего позиций: <b>{total}</b>\n"
    )


def watch_list_line(mark: str, name: str, status: str) -> str:
    return f"  {mark}  <code>{esc(name)}</code>  ·  {esc(status)}"


def watch_list_more(count: int) -> str:
    return f"\n<i>…и ещё {count} позиций.</i>"


WATCH_EMPTY = "<b>Список пуст.</b>"
WATCH_ADD_PROMPT = (
    "<b>МОДУЛЬ 03 · ДОБАВЛЕНИЕ</b>\n"
    f"{DOT}\n"
    "Направьте список имён (одно в строке)."
)
WATCH_DEL_PROMPT = (
    "<b>МОДУЛЬ 03 · УДАЛЕНИЕ</b>\n"
    f"{DOT}\n"
    "Направьте список имён для удаления (одно в строке)."
)


def watch_added(added: int, requested: int, total: int) -> str:
    return (
        f"<b>Добавлено:</b> {added} из {requested}.\n"
        f"Всего в списке: <b>{total}</b>"
    )


def watch_removed(removed: int, requested: int, total: int) -> str:
    return (
        f"<b>Удалено:</b> {removed} из {requested}.\n"
        f"Всего в списке: <b>{total}</b>"
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
        f"{DOT}\n"
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
        f"<b>ИСТОРИЯ · {esc(period)}</b>\n"
        f"{DOT}\n"
        f"Найдено записей: <b>{count}</b>\n"
    )


def found_list_line(index: int, name: str, score: int, when: str, claimed: bool) -> str:
    claimed_mark = "  ✓ занято" if claimed else ""
    return (
        f"  {index:>2}.  <code>{esc(name)}</code>  ·  "
        f"{score}/10  ·  {when}{claimed_mark}"
    )


FOUND_EMPTY_PERIOD = "История пуста за указанный период."
EXPORT_PREPARING = "Готовлю файл…"
EXPORT_EMPTY = "В истории пока нет записей для экспорта."
EXPORT_FAILED = "Не удалось отправить файл."


def export_caption(rows: int) -> str:
    return (
        f"<b>ЭКСПОРТ ИСТОРИИ</b>\n"
        f"{DOT}\n"
        f"Строк: <b>{rows}</b>\n"
        f"Файл содержит: имя, оценку, статус, источник, дату."
    )


# =============================================================================
#                          Общие сообщения
# =============================================================================

def limit_reached(limit: int) -> str:
    return (
        f"<b>ДОСТУП ОГРАНИЧЕН</b>\n"
        f"{DOT}\n"
        f"Установленный лимит сеансов исчерпан: "
        f"<b>{limit}</b> из <b>{limit}</b>.\n\n"
        f"Обратитесь к администратору для расширения квоты (модуль 06)."
    )


def flooded_pause(seconds: int) -> str:
    return (
        f"<b>ВРЕМЕННАЯ ПРИОСТАНОВКА ОБСЛУЖИВАНИЯ</b>\n"
        f"{DOT}\n"
        f"Все активные каналы проверки временно недоступны.\n\n"
        f"Ожидаемое восстановление: <b>{_eta(seconds)}</b>\n\n"
        f"<i>Сеанс не списан. Повторная попытка возможна после "
        f"завершения периода ожидания.</i>"
    )


def error_generic(reason: str) -> str:
    return (
        f"<b>ОБРАБОТКА ПРЕРВАНА</b>\n"
        f"{DOT}\n"
        f"<i>{reason}</i>\n\n"
        f"Вернитесь в панель управления и повторите операцию."
    )


def queue_wait_text(wait_hint: int) -> str:
    return (
        f"<b>СИСТЕМА ЗАГРУЖЕНА</b>\n"
        f"{DOT}\n"
        f"В данный момент выполняется обработка запроса другого "
        f"пользователя. Система допускает одну обработку одновременно — "
        f"это защищает каналы проверки от блокировок Telegram.\n\n"
        f"Повторите попытку через <b>{wait_hint} сек.</b>\n\n"
        f"<i>Сеанс не списан.</i>"
    )


# =============================================================================
#                            Обратная связь
# =============================================================================

def feedback_intro(max_per_hour: int, min_len: int = 5, max_len: int = 1000) -> str:
    return (
        f"<b>СВЯЗЬ С АДМИНИСТРАЦИЕЙ</b>\n"
        f"{DOT}\n"
        f"Напишите текст — он уйдёт напрямую администраторам.\n"
        f"Ответ придёт сюда же, в этот чат.\n\n"
        f"<b>Что можно написать:</b>\n"
        f"  ·  сообщить о проблеме или ошибке;\n"
        f"  ·  предложить улучшение;\n"
        f"  ·  задать вопрос по работе системы.\n\n"
        f"<b>Ограничения:</b>\n"
        f"  ·  длина: {min_len}-{max_len} символов;\n"
        f"  ·  не больше <b>{max_per_hour}</b> обращений в час.\n\n"
        f"<i>Введите текст следующим сообщением или нажмите «Отмена».</i>"
    )


def feedback_thanks(ticket_id: int) -> str:
    return (
        f"<b>ОБРАЩЕНИЕ ПРИНЯТО</b>\n"
        f"{DOT}\n"
        f"Номер обращения: <code>#{ticket_id}</code>\n\n"
        f"Администратор ответит в этот же чат. Обычно — в течение суток.\n\n"
        f"<i>Спасибо, что помогаете проекту.</i>"
    )


def feedback_rate_limited(minutes: int, max_per_hour: int = 3) -> str:
    return (
        f"<b>СЛИШКОМ ЧАСТО</b>\n"
        f"{DOT}\n"
        f"Лимит обращений — не больше {max_per_hour} в час.\n\n"
        f"Следующее можно отправить через <b>{minutes} мин.</b>"
    )


def feedback_invalid_length(min_len: int = 5, max_len: int = 1000) -> str:
    return (
        f"<b>НЕКОРРЕКТНАЯ ДЛИНА</b>\n"
        f"{DOT}\n"
        f"Текст должен быть от {min_len} до {max_len} символов. "
        f"Пришлите ещё раз или нажмите «Отмена»."
    )


FEEDBACK_TEXT_EXPECTED = (
    "Ожидается текстовое сообщение. Пришлите текст или нажмите «Отмена»."
)
ADMINS_UNAVAILABLE = (
    "К сожалению, администраторы недоступны. Попробуйте позже."
)
FEEDBACK_DELIVERY_FAILED = (
    "Не удалось доставить обращение администраторам. Попробуйте позже."
)


def admin_feedback_header(
    ticket_id: int, user_id: int, username: str, first_name: str
) -> str:
    """Заголовок, который видит админ в личке."""
    uname = f"@{esc(username)}" if username else "без username"
    name = esc(first_name) or "—"
    return (
        f"<b>ОБРАЩЕНИЕ #{ticket_id}</b>\n"
        f"{LINE}\n\n"
        f"<b>От:</b> {name}\n"
        f"<b>Кто:</b> <code>{uname}</code>\n"
        f"<b>ID:</b> <code>{user_id}</code>\n\n"
        f"<b>Сообщение:</b>\n"
    )


def admin_feedback_footer() -> str:
    return (
        "\n\n<i>Чтобы ответить — сделайте reply на это сообщение.\n"
        "Ответ уйдёт пользователю.</i>"
    )


def admin_feedback_message(
    ticket_id: int, user_id: int, username: str, first_name: str, body: str
) -> str:
    """Полное сообщение админу: шапка + экранированный текст + подвал."""
    return (
        admin_feedback_header(ticket_id, user_id, username, first_name)
        + esc(body)
        + admin_feedback_footer()
    )


ADMIN_REPLY_EMPTY = "Пустой ответ — не отправляю. Ответ должен быть текстом."
ADMIN_REPLY_FAILED = (
    "✗ Не удалось доставить ответ. Возможно, пользователь заблокировал бота."
)


def admin_reply_to_user(ticket_id: int, answer: str) -> str:
    return (
        f"<b>ОТВЕТ ОТ АДМИНИСТРАЦИИ</b>\n"
        f"{DOT}\n"
        f"<i>по обращению #{ticket_id}</i>\n\n"
        f"{esc(answer)}"
    )


def admin_reply_delivered(user_id: int, ticket_id: int) -> str:
    return (
        f"✓ Ответ по обращению #{ticket_id} доставлен пользователю "
        f"<code>{user_id}</code>."
    )


FEEDBACK_EMPTY = "Обращений пока нет."
FEEDBACK_STATUS_MARKS = {"answered": "✓", "sent": "•", "failed": "✗"}


def feedback_list_header() -> list[str]:
    return ["<b>ПОСЛЕДНИЕ ОБРАЩЕНИЯ</b>", DOT, ""]


def feedback_list_line(
    ticket_id: int, user_id: int, when: str, status: str, preview: str
) -> str:
    mark = FEEDBACK_STATUS_MARKS.get(status, "?")
    return (
        f"  {mark}  <b>#{ticket_id}</b>  "
        f"<code>{user_id}</code>  ·  {when}\n"
        f"       <i>{esc(preview)}…</i>"
    )


# =============================================================================
#                         Команды операторов
# =============================================================================

REPORT_PREPARING = "Формирую сводку…"
BACKUP_PREPARING = "Создаю бэкап, это может занять до минуты…"


def report_failed(error_name: str) -> str:
    return f"Не удалось отправить сводку: <code>{esc(error_name)}</code>"


def backup_failed(error_name: str) -> str:
    return f"Не удалось создать бэкап: <code>{esc(error_name)}</code>"


def backup_done(
    files: int, size_mb: float, path: str, removed: int, secrets_included: bool
) -> str:
    secrets = (
        "<b>включены</b> (.env и *.session)"
        if secrets_included
        else "исключены (BACKUP_INCLUDE_SECRETS=0)"
    )
    text = (
        f"<b>BACKUP · ВЫПОЛНЕН</b>\n"
        f"{DOT}\n"
        f"файлов в архиве: <b>{files}</b>\n"
        f"размер: <b>{size_mb} МБ</b>\n"
        f"секреты: {secrets}\n"
        f"путь: <code>{esc(path)}</code>"
    )
    if removed:
        text += f"\n\n<i>удалено старых: {removed}</i>"
    return text


def today_stats(
    records: int,
    unique_users: int,
    st: dict[str, int],
    alive: int,
    total_sessions: int,
) -> str:
    return (
        f"<b>СТАТИСТИКА ЗА 24 ЧАСА</b>\n"
        f"{DOT}\n\n"
        f"Записей в истории: <b>{records}</b>\n"
        f"Уникальных пользователей: <b>{unique_users}</b>\n\n"
        f"<b>С момента запуска:</b>\n"
        f"    Свободных найдено: <b>{st.get('free', 0)}</b>\n"
        f"    Занятых отсеяно:  <b>{st.get('taken', 0)}</b>\n"
        f"    HTTP-отсев:       <b>{st.get('http_short_circuit', 0)}</b>\n"
        f"    Попаданий в кэш:  <b>{st.get('cache_hits', 0)}</b>\n"
        f"    Флудов получено:  <b>{st.get('floods', 0)}</b>\n"
        f"    Сессий живых:     <b>{alive} / {total_sessions}</b>"
    )


def _session_line(row: dict[str, object]) -> str:
    """row: name, alive, calls_made, times_flooded, paused_for."""
    mark = "✓" if row.get("alive", True) else "✗ недоступна"
    paused = int(row.get("paused_for", 0) or 0)
    pause_note = f", пауза {paused} с" if paused > 0 else ""
    return (
        f"  {mark}  <code>{esc(str(row.get('name', '?')))}</code>\n"
        f"       запросов: <b>{row.get('calls_made', 0)}</b>, "
        f"флудов: <b>{row.get('times_flooded', 0)}</b>{pause_note}"
    )


def sessions_state(rows: list[dict[str, object]]) -> str:
    lines = ["<b>СОСТОЯНИЕ СЕССИЙ</b>", DOT, ""]
    lines.extend(_session_line(r) for r in rows)
    if not rows:
        lines.append("<i>Сессий нет.</i>")
    return "\n".join(lines)


def service_state(
    sessions: list[dict[str, object]],
    st: dict[str, int],
    storage_stats: dict[str, int],
    watch: dict[str, int],
    cache: dict[str, int],
    feedback_count: int,
    beta_mode: bool,
    invited: int,
    quota_limit: int,
) -> str:
    alive = sum(1 for s in sessions if s.get("alive", True))
    lines = ["<b>СОСТОЯНИЕ СЕРВИСА</b>", DOT, ""]
    lines.append(f"<b>Сессии:</b> <b>{alive}</b> из <b>{len(sessions)}</b> живы")
    lines.extend(_session_line(s) for s in sessions)
    lines += [
        "",
        "<b>Проверки:</b>",
        f"    HTTP-отсев:  <b>{st.get('http_short_circuit', 0)}</b>",
        f"    Кэш:         <b>{st.get('cache_hits', 0)}</b> "
        f"(записей: {cache.get('total', 0)})",
        f"    Свободно:    <b>{st.get('free', 0)}</b>",
        f"    За TON:      <b>{st.get('fragment', 0)}</b>",
        f"    Занято:      <b>{st.get('taken', 0)}</b>",
        f"    Ошибок:      <b>{st.get('error', 0)}</b>",
        "",
        "<b>Хранилище:</b>",
        f"    Всего:  <b>{storage_stats.get('total', 0)}</b>,  "
        f"свободных: <b>{storage_stats.get('free', 0)}</b>,  "
        f"занято пользователями: <b>{storage_stats.get('claimed', 0)}</b>",
        "",
        "<b>Watchlist:</b>",
        f"    Всего: <b>{watch.get('total', 0)}</b>  "
        f"(hot {watch.get('hot', 0)}, warm {watch.get('warm', 0)}, "
        f"cold {watch.get('cold', 0)}, sleep {watch.get('sleeping', 0)})",
        "",
        f"<b>Обращения:</b> {feedback_count}",
        "",
        "<b>Доступ:</b>",
        f"    закрытая бета: {'да' if beta_mode else 'нет'}  ·  "
        f"приглашено: <b>{invited}</b>  ·  лимит сеансов: <b>{quota_limit}</b>",
    ]
    return "\n".join(lines)


QUOTA_USAGE_HINT = (
    "Формат:\n"
    "  <code>/quota &lt;user_id&gt;</code> — посмотреть;\n"
    "  <code>/quota_reset &lt;user_id&gt;</code> — обнулить использованные сеансы;\n"
    "  <code>/quota_add &lt;user_id&gt; &lt;n&gt;</code> — добавить n сеансов."
)


def quota_info(user_id: int, used: int, remaining: int, limit: int) -> str:
    return (
        f"<b>КВОТА</b>\n"
        f"{DOT}\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Использовано: <b>{used}</b>  ·  осталось: <b>{remaining}</b> / {limit}"
    )


def quota_reset_done(user_id: int, limit: int) -> str:
    return (
        f"✓ Квота пользователя <code>{user_id}</code> сброшена: "
        f"доступно <b>{limit}</b> сеансов."
    )
