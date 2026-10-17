"""
Самопроверка установки UserNeed.

Зачем: файлы проекта часто переносят вручную (копипаст, частичное
обновление), и тогда версии расходятся — например, новый main.py
передаёт в чекер параметр `retries`, а старый checker.py его не знает,
и бот падает на старте. Этот скрипт за секунду показывает, какие
модули отстали, не подключаясь к Telegram и не читая .env.

Запуск:
    python selfcheck.py

Код возврата: 0 — всё на месте, 1 — есть устаревшие файлы.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable
from typing import Any

OK = "✓"
BAD = "✗"

Check = tuple[str, Callable[[], Any]]


def _has_attr(module: str, attr: str) -> Callable[[], Any]:
    def check() -> Any:
        mod = importlib.import_module(module)
        if not hasattr(mod, attr):
            raise AttributeError(f"нет {module}.{attr}")
        return getattr(mod, attr)

    return check


def _class_has_method(module: str, cls: str, method: str) -> Callable[[], Any]:
    def check() -> Any:
        mod = importlib.import_module(module)
        obj = getattr(mod, cls)
        if not hasattr(obj, method):
            raise AttributeError(f"нет {cls}.{method}()")

    return check


def _signature_has(module: str, attr: str, param: str) -> Callable[[], Any]:
    def check() -> Any:
        mod = importlib.import_module(module)
        obj = getattr(mod, attr)
        if param not in inspect.signature(obj).parameters:
            raise TypeError(f"{module}.{attr} без параметра {param!r}")

    return check


def _config_fields() -> Any:
    from dataclasses import fields

    from config import Config

    names = {f.name for f in fields(Config)}
    missing = {"check_retries", "name_length", "bulk_limit"} - names
    if missing:
        raise TypeError(f"в Config нет полей: {', '.join(sorted(missing))}")


def _backup_files() -> Any:
    from services.backup_manager import DB_FILES

    if "settings.db" not in DB_FILES:
        raise AttributeError("settings.db не попадает в бэкапы")


# (человекочитаемая проверка, что именно проверяем, функция)
CHECKS: list[tuple[str, str, Callable[[], Any]]] = [
    (
        "config.py",
        "новые ключи CHECK_RETRIES / NAME_LENGTH / BULK_LIMIT",
        _config_fields,
    ),
    (
        "services/checker.py",
        "параметр retries в TelegramChecker",
        _signature_has("services.checker", "TelegramChecker", "retries"),
    ),
    (
        "services/checker.py",
        "метод apply_runtime() для панели /params",
        _class_has_method("services.checker", "TelegramChecker", "apply_runtime"),
    ),
    (
        "services/generator.py",
        "длины 5–8 (MIN_LENGTH / MAX_LENGTH)",
        _has_attr("services.generator", "MAX_LENGTH"),
    ),
    (
        "services/generator.py",
        "generate_many(length=...)",
        _signature_has("services.generator", "generate_many", "length"),
    ),
    (
        "services/runtime_settings.py",
        "RuntimeSettings",
        _has_attr("services.runtime_settings", "RuntimeSettings"),
    ),
    (
        "bot/keyboards.py",
        "settings_panel_kb()",
        _has_attr("bot.keyboards", "settings_panel_kb"),
    ),
    (
        "bot/branding.py",
        "тексты панели settings_panel()",
        _has_attr("bot.branding", "settings_panel"),
    ),
    (
        "bot/branding.py",
        "help_text(..., is_admin)",
        _signature_has("bot.branding", "help_text", "is_admin"),
    ),
    (
        "bot/handlers.py",
        "состояние SettingsStates",
        _has_attr("bot.handlers", "SettingsStates"),
    ),
    (
        "bot/handlers.py",
        "_generation_body(..., settings)",
        _signature_has("bot.handlers", "_generation_body", "settings"),
    ),
    (
        "services/backup_manager.py",
        "settings.db в списке бэкапа",
        _backup_files,
    ),
]


def main() -> int:
    print("UserNeed · самопроверка установки")
    print("=" * 60)

    failed: list[tuple[str, str, str]] = []
    current_file = ""

    for filename, description, check in CHECKS:
        if filename != current_file:
            current_file = filename
            print(f"\n{filename}")
        try:
            check()
        except Exception as exc:  # noqa: BLE001 — отчёт, а не падение
            print(f"  {BAD} {description}")
            print(f"      {type(exc).__name__}: {exc}")
            failed.append((filename, description, str(exc)))
        else:
            print(f"  {OK} {description}")

    print("\n" + "=" * 60)
    if not failed:
        print("Всё на месте: файлы одной ревизии.")
        runtime = importlib.import_module("services.checker")
        sig_ok = "retries" in inspect.signature(
            runtime.TelegramChecker.__init__
        ).parameters
        if sig_ok:
            print("Следующий шаг: python main.py")
        return 0

    stale = list(dict.fromkeys(filename for filename, _, _ in failed))
    print(f"Устаревших файлов: {len(stale)}")
    for filename in stale:
        print(f"  · {filename}")
    print(
        "\nЗамените эти файлы целиком из репозитория "
        "(ветка arena/88dd6da1-userneed) и запустите проверку снова."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
