"""Inline-клавиатуры UserNeed."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Подписи кнопок — единственные «тексты» вне branding.py: они жёстко
# привязаны к callback_data и нигде больше не используются.

BTN_BACK = "Возврат в панель"
BTN_CANCEL = "Отмена"
BTN_PREV = "Назад"


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Главное меню. Модуль 03 (мониторинг) показываем только операторам."""
    kb = InlineKeyboardBuilder()
    kb.button(text="01  ·  Генерация имён",      callback_data="gen:start")
    kb.button(text="02  ·  Пакетная проверка",   callback_data="bulk:start")
    if is_admin:
        kb.button(text="03  ·  Мониторинг",      callback_data="watch:start")
    kb.button(text="04  ·  История находок",     callback_data="found:show")
    kb.button(text="05  ·  Регламент",           callback_data="help")
    kb.button(text="06  ·  Связаться с админом", callback_data="feedback:start")
    if is_admin:
        kb.button(text="07  ·  Параметры прогонов", callback_data="settings:open")
    kb.adjust(1)
    return kb.as_markup()


def gen_mode_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="01  ·  Осмысленные имена",   callback_data="gen:mode:smart")
    kb.button(text="02  ·  Случайные сочетания", callback_data="gen:mode:random")
    kb.button(text="03  ·  Похожие на ...",      callback_data="gen:mode:similar")
    kb.button(text=BTN_BACK,                     callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def _count_options(options: list[int], max_allowed: int) -> list[int]:
    out = [n for n in options if n <= max_allowed]
    return out or [max_allowed]


def gen_count_menu(mode: str, max_allowed: int) -> InlineKeyboardMarkup:
    options = _count_options([3, 5, 10] if mode == "smart" else [5, 10, 20], max_allowed)
    kb = InlineKeyboardBuilder()
    for n in options:
        kb.button(text=str(n), callback_data=f"gen:run:{mode}:{n}")
    kb.button(text=BTN_PREV, callback_data="gen:start")
    kb.adjust(len(options), 1)
    return kb.as_markup()


def gen_similar_count_menu(base: str, max_allowed: int) -> InlineKeyboardMarkup:
    options = _count_options([5, 10, 15, 20], max_allowed)
    kb = InlineKeyboardBuilder()
    for n in options:
        kb.button(text=str(n), callback_data=f"gen:sim:{base}:{n}")
    kb.button(text=BTN_PREV, callback_data="gen:start")
    kb.adjust(len(options), 1)
    return kb.as_markup()


def gen_progress_kb(found_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if found_count > 0:
        kb.button(
            text=f"Завершить обработку · результат: {found_count}",
            callback_data="gen:stop",
        )
    else:
        kb.button(text="Прервать обработку", callback_data="gen:stop")
    kb.adjust(1)
    return kb.as_markup()


def bulk_progress_kb() -> InlineKeyboardMarkup:
    """Та же кнопка остановки — пакетная проверка использует общий механизм."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Прервать обработку", callback_data="gen:stop")
    kb.adjust(1)
    return kb.as_markup()


def found_actions_kb(found: list[tuple[str, int, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    shown = found[:5]
    for name, _, _ in shown:
        kb.button(text=f"✓ Занял @{name}", callback_data=f"ack_claim:{name}")
        kb.button(text=f"↻ Похожие на {name}", callback_data=f"sim_base:{name}")
    kb.button(text=BTN_BACK, callback_data="menu:main")
    kb.adjust(*([2] * len(shown) + [1]))
    return kb.as_markup()


def watch_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Показать список",  callback_data="watch:list")
    kb.button(text="Добавить имена",   callback_data="watch:add")
    kb.button(text="Удалить имя",      callback_data="watch:del")
    kb.button(text=BTN_BACK,           callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def found_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Последние 30",   callback_data="found:list:30")
    kb.button(text="За 7 дней",      callback_data="found:list:7d")
    kb.button(text="За 30 дней",     callback_data="found:list:30d")
    kb.button(text="Экспорт в CSV",  callback_data="found:export")
    kb.button(text=BTN_BACK,         callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_BACK, callback_data="menu:main")
    return kb.as_markup()


def cancel_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_CANCEL, callback_data="menu:main")
    return kb.as_markup()


# =============================================================================
#                    Модуль 07 · Параметры (только операторы)
# =============================================================================

def settings_panel_kb(overrides: int) -> InlineKeyboardMarkup:
    """Панель: две группы, журнал и (при наличии) полный сброс."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Проверка",          callback_data="settings:group:check")
    kb.button(text="Генерация",         callback_data="settings:group:gen")
    kb.button(text="Журнал изменений",  callback_data="settings:log")
    if overrides:
        kb.button(text=f"Сбросить все · {overrides}", callback_data="settings:reset_all")
    kb.button(text=BTN_BACK,            callback_data="menu:main")
    kb.adjust(2, 1, 1, 1, 1)
    return kb.as_markup()


def settings_group_kb(group: str, items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """items — пары (ключ параметра, подпись кнопки с текущим значением)."""
    kb = InlineKeyboardBuilder()
    for key, text in items:
        kb.button(text=text, callback_data=f"settings:edit:{key}")
    kb.button(text=BTN_BACK, callback_data="settings:open")
    kb.adjust(1)
    return kb.as_markup()


def settings_param_kb(
    key: str,
    group: str,
    *,
    is_bool: bool,
    step_text: str,
    has_override: bool,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_bool:
        kb.button(text="Переключить", callback_data=f"settings:toggle:{key}")
    else:
        kb.button(text=f"− {step_text}", callback_data=f"settings:dec:{key}")
        kb.button(text=f"+ {step_text}", callback_data=f"settings:inc:{key}")
        kb.button(text="Ввести значение", callback_data=f"settings:ask:{key}")
    if has_override:
        kb.button(text="Сбросить к .env", callback_data=f"settings:reset:{key}")
    kb.button(text=BTN_BACK, callback_data=f"settings:group:{group}")
    if is_bool:
        kb.adjust(1, 1, 1)
    else:
        kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


def settings_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, сбросить всё", callback_data="settings:reset_all_yes")
    kb.button(text=BTN_CANCEL,         callback_data="settings:open")
    kb.adjust(1)
    return kb.as_markup()


def settings_back_kb(group: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    target = f"settings:group:{group}" if group else "settings:open"
    kb.button(text=BTN_BACK, callback_data=target)
    return kb.as_markup()
