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
