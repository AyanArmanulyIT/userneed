"""
RuntimeSettings — параметры прогонов, которые оператор меняет из бота.

Два уровня конфигурации:
  • .env (config.Config) — значения по умолчанию, «заводские»;
  • settings.db         — переопределения из панели /params.

Переопределение хранится в SQLite и переживает перезапуск. Значения
читаются на каждом прогоне, поэтому изменения вступают в силу сразу,
без рестарта: чекер получает новые ретраи/паузы/параллельность через
apply_runtime(), а генерация — длину и лимиты при следующем запуске.

Каждое изменение пишется в settings_audit: кто, когда и что поменял.
Это единственный источник истины для журнала в панели оператора.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config

logger = logging.getLogger(__name__)

# Порядок групп в панели. Подписи живут в bot/branding.py.
GROUPS: tuple[str, ...] = ("check", "gen")


class SettingsError(ValueError):
    """Ошибка валидации значения. Код переводит branding в текст."""

    def __init__(
        self,
        code: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.minimum = minimum
        self.maximum = maximum


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Описание одного настраиваемого параметра."""

    key: str
    group: str
    kind: str          # "int" | "float" | "bool"
    minimum: float
    maximum: float
    step: float
    default: Any
    digits: int = 0

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


def build_specs(cfg: Config) -> dict[str, ParamSpec]:
    """Собирает реестр параметров. default берётся из .env через Config."""
    specs = (
        # --- Проверка ---------------------------------------------------------
        ParamSpec(
            "check_retries", "check", "int", 1, 12, 1,
            max(1, min(12, cfg.check_retries)),
        ),
        ParamSpec(
            "check_concurrency", "check", "int", 1, 16, 1,
            max(1, min(16, cfg.check_concurrency)),
        ),
        ParamSpec(
            "check_delay_min", "check", "float", 0.0, 10.0, 0.5,
            max(0.0, min(10.0, cfg.check_delay_min)), digits=1,
        ),
        ParamSpec(
            "check_delay_max", "check", "float", 0.0, 15.0, 0.5,
            max(0.0, min(15.0, cfg.check_delay_max)), digits=1,
        ),
        ParamSpec(
            "http_precheck", "check", "bool", 0, 1, 1,
            bool(cfg.http_precheck_enabled),
        ),
        # --- Генерация --------------------------------------------------------
        ParamSpec(
            "name_length", "gen", "int", 5, 8, 1,
            max(5, min(8, cfg.name_length)),
        ),
        ParamSpec(
            "filter_min_score", "gen", "int", 1, 10, 1,
            max(1, min(10, cfg.filter_min_score)),
        ),
        ParamSpec(
            "gen_max_smart", "gen", "int", 3, 30, 1,
            max(3, min(30, cfg.gen_max_smart)),
        ),
        ParamSpec(
            "gen_max_random", "gen", "int", 5, 50, 5,
            max(5, min(50, cfg.gen_max_random)),
        ),
        ParamSpec(
            "bulk_limit", "gen", "int", 10, 2000, 50,
            max(10, min(2000, cfg.bulk_limit)),
        ),
    )
    return {spec.key: spec for spec in specs}


class RuntimeSettings:
    """SQLite-хранилище переопределений + живое применение к чекеру."""

    def __init__(self, path: Path | str, cfg: Config) -> None:
        self._path = Path(path)
        self._specs = build_specs(cfg)
        self._overrides: dict[str, str] = {}
        self._checker: Any | None = None
        self._db: sqlite3.Connection | None = None
        try:
            self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
            self._load()
            logger.info(
                "Параметры прогонов: %s (переопределений: %d)",
                self._path, len(self._overrides),
            )
        except sqlite3.Error as exc:
            logger.exception("Не удалось открыть %s: %s", self._path, exc)
            self._db = None

    # --- Схема / загрузка ----------------------------------------------------

    def _init_schema(self) -> None:
        assert self._db is not None
        with self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_by INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings_audit (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    key        TEXT NOT NULL,
                    old_value  TEXT NOT NULL,
                    new_value  TEXT NOT NULL,
                    user_id    INTEGER NOT NULL DEFAULT 0,
                    actor      TEXT NOT NULL DEFAULT '',
                    ts         REAL NOT NULL
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_settings_audit_ts "
                "ON settings_audit(ts)"
            )

    def _load(self) -> None:
        if self._db is None:
            return
        try:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        except sqlite3.Error as exc:
            logger.exception("runtime_settings._load упал: %s", exc)
            return
        for row in rows:
            key = str(row["key"])
            if key in self._specs:
                self._overrides[key] = str(row["value"])

    # --- Реестр --------------------------------------------------------------

    def spec(self, key: str) -> ParamSpec:
        try:
            return self._specs[key]
        except KeyError as exc:
            raise SettingsError("unknown") from exc

    def specs(self) -> list[ParamSpec]:
        return list(self._specs.values())

    def grouped(self) -> dict[str, list[ParamSpec]]:
        return {
            group: [s for s in self._specs.values() if s.group == group]
            for group in GROUPS
        }

    # --- Чтение --------------------------------------------------------------

    def get(self, key: str) -> Any:
        spec = self.spec(key)
        raw = self._overrides.get(key)
        if raw is None:
            return spec.default
        return self._decode(spec, raw)

    def value_from_raw(self, key: str, raw: str) -> Any:
        """Разбирает сохранённое в БД значение (для журнала и отладки)."""
        return self._decode(self.spec(key), raw)

    def has_override(self, key: str) -> bool:
        return key in self._overrides

    def override_count(self) -> int:
        return len(self._overrides)

    def snapshot(self) -> dict[str, Any]:
        return {key: self.get(key) for key in self._specs}

    @property
    def check_retries(self) -> int:
        return int(self.get("check_retries"))

    @property
    def check_concurrency(self) -> int:
        return int(self.get("check_concurrency"))

    @property
    def check_delay_min(self) -> float:
        return float(self.get("check_delay_min"))

    @property
    def check_delay_max(self) -> float:
        return float(self.get("check_delay_max"))

    @property
    def http_precheck(self) -> bool:
        return bool(self.get("http_precheck"))

    @property
    def name_length(self) -> int:
        return int(self.get("name_length"))

    @property
    def filter_min_score(self) -> int:
        return int(self.get("filter_min_score"))

    @property
    def gen_max_smart(self) -> int:
        return int(self.get("gen_max_smart"))

    @property
    def gen_max_random(self) -> int:
        return int(self.get("gen_max_random"))

    @property
    def bulk_limit(self) -> int:
        return int(self.get("bulk_limit"))

    # --- Запись --------------------------------------------------------------

    def set(
        self,
        key: str,
        value: Any,
        user_id: int = 0,
        actor: str = "",
    ) -> Any:
        """
        Валидирует и сохраняет значение. Возвращает приведённое значение.

        Если новое значение совпадает с default из .env — переопределение
        удаляется (панель снова показывает «из .env»).
        """
        spec = self.spec(key)
        coerced = self._coerce(spec, value)
        self._check_order(spec, coerced)

        old_value = self.get(key)
        old_display = self._display(spec, old_value)
        new_display = self._display(spec, coerced)

        if coerced == spec.default:
            self._delete_row(key)
            self._overrides.pop(key, None)
        else:
            raw = self._encode(spec, coerced)
            self._write_row(key, raw, user_id)
            self._overrides[key] = raw

        self._audit(key, old_display, new_display, user_id, actor)
        self._apply_checker()
        logger.info(
            "Параметр %s: %s → %s (user=%s)", key, old_display, new_display, user_id,
        )
        return coerced

    def reset(self, key: str, user_id: int = 0, actor: str = "") -> bool:
        """Сбрасывает переопределение к значению из .env. False — его не было."""
        spec = self.spec(key)
        if key not in self._overrides:
            return False
        old_display = self._display(spec, self.get(key))
        new_display = self._display(spec, spec.default)
        self._delete_row(key)
        self._overrides.pop(key, None)
        self._audit(key, old_display, new_display, user_id, actor)
        self._apply_checker()
        logger.info("Параметр %s сброшен к .env (user=%s)", key, user_id)
        return True

    def reset_all(self, user_id: int = 0, actor: str = "") -> int:
        """Сбрасывает все переопределения. Возвращает их количество."""
        keys = list(self._overrides)
        for key in keys:
            self.reset(key, user_id, actor)
        return len(keys)

    # --- Журнал --------------------------------------------------------------

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        try:
            rows = self._db.execute(
                """
                SELECT key, old_value, new_value, user_id, actor, ts
                FROM settings_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.exception("runtime_settings.history упал: %s", exc)
            return []
        return [dict(row) for row in rows]

    def last_change(self) -> dict[str, Any] | None:
        rows = self.history(1)
        return rows[0] if rows else None

    # --- Чекер ---------------------------------------------------------------

    def bind_checker(self, checker: Any) -> None:
        """Привязывает чекер и сразу применяет текущие значения."""
        self._checker = checker
        self._apply_checker()

    def _apply_checker(self) -> None:
        if self._checker is None:
            return
        try:
            self._checker.apply_runtime(
                retries=self.check_retries,
                concurrency=self.check_concurrency,
                delay_min=self.check_delay_min,
                delay_max=self.check_delay_max,
                http_precheck=self.http_precheck,
            )
        except Exception:  # noqa: BLE001 — панель не должна ронять бота
            logger.exception("Не удалось применить параметры к чекеру")

    # --- Валидация / кодирование --------------------------------------------

    def _coerce(self, spec: ParamSpec, value: Any) -> Any:
        if spec.kind == "bool":
            result = self._coerce_bool(value)
        elif spec.kind == "int":
            result = self._coerce_int(value)
        elif spec.kind == "float":
            result = self._coerce_float(spec, value)
        else:  # pragma: no cover — защита от опечатки в реестре
            raise SettingsError("unknown")

        if spec.kind != "bool" and not spec.contains(float(result)):
            raise SettingsError(
                "range", minimum=spec.minimum, maximum=spec.maximum
            )
        return result

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "on", "yes", "да", "вкл"}:
            return True
        if text in {"0", "false", "off", "no", "нет", "выкл"}:
            return False
        raise SettingsError("bool")

    @staticmethod
    def _coerce_int(value: Any) -> int:
        if isinstance(value, bool):
            raise SettingsError("int")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise SettingsError("int")
            return int(value)
        text = str(value).strip().replace(" ", "")
        try:
            return int(text)
        except ValueError as exc:
            raise SettingsError("int") from exc

    @staticmethod
    def _coerce_float(spec: ParamSpec, value: Any) -> float:
        if isinstance(value, bool):
            raise SettingsError("float")
        try:
            result = float(str(value).strip().replace(",", "."))
        except ValueError as exc:
            raise SettingsError("float") from exc
        return round(result, spec.digits)

    def _check_order(self, spec: ParamSpec, value: Any) -> None:
        """Пауза min не может стать больше паузы max (и наоборот)."""
        if spec.key == "check_delay_min" and float(value) > self.check_delay_max:
            raise SettingsError("delay_order")
        if spec.key == "check_delay_max" and float(value) < self.check_delay_min:
            raise SettingsError("delay_order")

    @staticmethod
    def _decode(spec: ParamSpec, raw: str) -> Any:
        if spec.kind == "bool":
            return raw == "1"
        if spec.kind == "int":
            try:
                return int(raw)
            except ValueError:
                return spec.default
        try:
            return float(raw)
        except ValueError:
            return spec.default

    @staticmethod
    def _encode(spec: ParamSpec, value: Any) -> str:
        if spec.kind == "bool":
            return "1" if value else "0"
        if spec.kind == "int":
            return str(int(value))
        return f"{float(value):.{spec.digits}f}"

    @staticmethod
    def _display(spec: ParamSpec, value: Any) -> str:
        if spec.kind == "bool":
            return "1" if value else "0"
        if spec.kind == "int":
            return str(int(value))
        return f"{float(value):.{spec.digits}f}"

    # --- SQLite --------------------------------------------------------------

    def _write_row(self, key: str, raw: str, user_id: int) -> None:
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO settings (key, value, updated_by, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key)
                    DO UPDATE SET
                        value = excluded.value,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (key, raw, user_id, time.time()),
                )
        except sqlite3.Error as exc:
            logger.exception("runtime_settings._write_row(%s) упал: %s", key, exc)

    def _delete_row(self, key: str) -> None:
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
        except sqlite3.Error as exc:
            logger.exception("runtime_settings._delete_row(%s) упал: %s", key, exc)

    def _audit(
        self,
        key: str,
        old_value: str,
        new_value: str,
        user_id: int,
        actor: str,
    ) -> None:
        if self._db is None:
            return
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO settings_audit
                        (key, old_value, new_value, user_id, actor, ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, old_value, new_value, user_id, actor[:64], time.time()),
                )
        except sqlite3.Error as exc:
            logger.exception("runtime_settings._audit(%s) упал: %s", key, exc)

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None
