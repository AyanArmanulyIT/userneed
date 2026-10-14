"""
Конфигурация приложения. Все параметры читаются из .env.

Правила:
  • load_dotenv() из python-dotenv — читает .env рядом с этим файлом
    (а не в текущей папке), поэтому бот можно запускать из любого cwd.
  • Типы приводятся с валидацией, ошибки — понятные.
  • Если обязательное поле пусто — бот не стартует в «полу-рабочем» виде.
  • Опасные, но допустимые комбинации (пустой whitelist в бете, нет
    админов) не роняют старт, а попадают в runtime_warnings() — main.py
    пишет их в лог, `python -m config` печатает в консоль.

Пути:
  • PROJECT_ROOT — папка с config.py.
  • cfg.data_dir  — где лежат все *.db, *.session, logs/, backups/.
    По умолчанию = PROJECT_ROOT (как было раньше при запуске из корня).
    Задаётся через DATA_DIR, если хочется держать данные отдельно от кода.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

load_dotenv(PROJECT_ROOT / ".env")


# =============================================================================
#                        Помощники чтения типов
# =============================================================================

def _get_int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        if default is None:
            raise RuntimeError(f"❌ Переменная окружения {name} не задана")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"❌ {name} должно быть целым, получено: {raw!r}") from exc


def _get_float(name: str, default: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        if default is None:
            raise RuntimeError(f"❌ Переменная окружения {name} не задана")
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"❌ {name} должно быть числом, получено: {raw!r}") from exc


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return default if raw == "" else raw in {"1", "true", "yes", "on"}


def _get_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _parse_int_set(raw: str, name: str) -> frozenset[int]:
    """'111, 222, 333' → frozenset({111, 222, 333})."""
    raw = raw.replace(" ", "")
    if not raw:
        return frozenset()
    out: set[int] = set()
    for token in raw.split(","):
        if not token:
            continue
        try:
            out.add(int(token))
        except ValueError as exc:
            raise RuntimeError(
                f"❌ {name}: ожидалось число, получено: {token!r}"
            ) from exc
    return frozenset(out)


def _parse_str_list(raw: str, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
    """' a,b ,,c ' → ('a','b','c'). Пусто → fallback."""
    raw = raw.strip()
    if not raw:
        return fallback
    seen: set[str] = set()
    out: list[str] = []
    for token in raw.split(","):
        name = token.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _resolve_path(raw: str, default: Path) -> Path:
    """Пустое значение → default. Относительный путь — от корня проекта."""
    if not raw:
        return default
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p


def _get_banner_path() -> str:
    """
    BANNER_PATH — URL или локальный путь.
    URL возвращается как есть. Локальный путь нормализуется относительно
    корня проекта.
    """
    raw = _get_str("BANNER_PATH")
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://")):
        return raw
    return str(_resolve_path(raw, PROJECT_ROOT))


# =============================================================================
#                              Config
# =============================================================================

@dataclass(frozen=True, slots=True)
class Config:
    # Aiogram
    bot_token: str
    admin_ids: frozenset[int]
    usage_limit: int

    # Закрытая бета
    beta_mode: bool
    allowed_user_ids: frozenset[int]

    # Telethon
    api_id: int
    api_hash: str
    session_names: tuple[str, ...]

    # Чекер
    check_concurrency: int
    flood_cooldown_extra: int
    flood_park_threshold: int
    check_delay_min: float
    check_delay_max: float
    http_precheck_enabled: bool

    # Лимиты генерации
    global_gen_concurrency: int
    gen_max_smart: int
    gen_max_random: int
    gen_busy_retry_hint: int

    # Хранилище
    storage_enabled: bool
    found_history_days: int
    data_dir: Path

    # Генератор
    use_external_dict: bool
    similar_max_results: int
    filter_min_score: int

    # Watchlist
    watch_cold_interval: int
    watch_warm_interval: int
    watch_hot_interval: int
    watch_batch_size: int
    watch_sleep_after_days: int

    # Бэкапы
    backup_include_secrets: bool

    # Брендинг
    banner_path: str

    # --- Производные пути ---------------------------------------------------

    def db_path(self, name: str) -> Path:
        """Путь к файлу БД внутри data_dir: cfg.db_path('found.db')."""
        return self.data_dir / name

    def session_path(self, session_name: str) -> str:
        """Путь к Telethon-сессии (без расширения — Telethon добавит .session)."""
        return str(self.data_dir / session_name)

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def has_access(self, user_id: int) -> bool:
        if not self.beta_mode:
            return True
        return user_id in self.admin_ids or user_id in self.allowed_user_ids

    # --- Предупреждения, не блокирующие старт --------------------------------

    def runtime_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.admin_ids:
            warnings.append(
                "ADMIN_IDS пуст — админ-команды, мониторинг, обратная связь "
                "и алерты недоступны."
            )
        if self.beta_mode and not self.allowed_user_ids:
            warnings.append(
                "BETA_MODE=1, а ALLOWED_USER_IDS пуст — бот отвечает только "
                "администраторам. Остальные получают «доступ закрыт»."
            )
        if not self.beta_mode:
            warnings.append(
                "BETA_MODE=0 — бот открыт для всех. Убедитесь, что это осознанно."
            )
        if self.backup_include_secrets:
            warnings.append(
                "BACKUP_INCLUDE_SECRETS=1 — в архивы бэкапа попадают .env и "
                "*.session (полный доступ к аккаунтам). Храните архивы надёжно."
            )
        if len(self.session_names) == 1:
            warnings.append(
                "Задана одна Telethon-сессия — при FloodWait пул встанет целиком."
            )
        return warnings


def load_config() -> Config:
    bot_token = _get_str("BOT_TOKEN")
    api_hash = _get_str("API_HASH")

    if not bot_token:
        raise RuntimeError("❌ BOT_TOKEN не задан в .env")
    if not api_hash:
        raise RuntimeError("❌ API_HASH не задан в .env")

    session_names = _parse_str_list(
        _get_str("SESSION_NAMES") or _get_str("SESSION_NAME"),
        fallback=("acc1", "acc2"),
    )
    if not session_names:
        raise RuntimeError("❌ SESSION_NAMES пуст")

    delay_min = _get_float("CHECK_DELAY_MIN", 1.5)
    delay_max = _get_float("CHECK_DELAY_MAX", 3.0)
    if delay_max < delay_min:
        delay_min, delay_max = delay_max, delay_min

    hot = max(300, _get_int("WATCH_HOT_INTERVAL", 3600))
    warm = max(hot, _get_int("WATCH_WARM_INTERVAL", 14400))
    cold = max(warm, _get_int("WATCH_COLD_INTERVAL", 86400))

    # Нижние границы не даём занизить — иначе бот уйдёт в флуд.
    gen_max_smart = max(3, min(30, _get_int("GEN_MAX_SMART", 10)))
    gen_max_random = max(5, min(50, _get_int("GEN_MAX_RANDOM", 20)))

    data_dir = _resolve_path(_get_str("DATA_DIR"), PROJECT_ROOT)
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        bot_token=bot_token,
        admin_ids=_parse_int_set(_get_str("ADMIN_IDS"), "ADMIN_IDS"),
        usage_limit=max(1, _get_int("USAGE_LIMIT", 3)),

        beta_mode=_get_bool("BETA_MODE", True),
        allowed_user_ids=_parse_int_set(
            _get_str("ALLOWED_USER_IDS"), "ALLOWED_USER_IDS"
        ),

        api_id=_get_int("API_ID"),
        api_hash=api_hash,
        session_names=session_names,

        check_concurrency=max(1, _get_int("CHECK_CONCURRENCY", 4)),
        flood_cooldown_extra=max(0, _get_int("FLOOD_COOLDOWN_EXTRA", 3)),
        flood_park_threshold=max(30, _get_int("FLOOD_PARK_THRESHOLD", 300)),
        check_delay_min=max(0.0, delay_min),
        check_delay_max=max(0.0, delay_max),
        http_precheck_enabled=_get_bool("HTTP_PRECHECK_ENABLED", True),

        global_gen_concurrency=max(1, _get_int("GLOBAL_GEN_CONCURRENCY", 1)),
        gen_max_smart=gen_max_smart,
        gen_max_random=gen_max_random,
        gen_busy_retry_hint=max(10, _get_int("GEN_BUSY_RETRY_HINT", 45)),

        storage_enabled=_get_bool("STORAGE_ENABLED", True),
        found_history_days=max(1, _get_int("FOUND_HISTORY_DAYS", 30)),
        data_dir=data_dir,

        use_external_dict=_get_bool("USE_EXTERNAL_DICT", True),
        similar_max_results=max(5, _get_int("SIMILAR_MAX_RESULTS", 20)),
        filter_min_score=max(1, min(10, _get_int("FILTER_MIN_SCORE", 3))),

        watch_hot_interval=hot,
        watch_warm_interval=warm,
        watch_cold_interval=cold,
        watch_batch_size=max(5, _get_int("WATCH_BATCH_SIZE", 30)),
        watch_sleep_after_days=max(1, _get_int("WATCH_SLEEP_AFTER_DAYS", 20)),

        backup_include_secrets=_get_bool("BACKUP_INCLUDE_SECRETS", False),

        banner_path=_get_banner_path(),
    )


if __name__ == "__main__":
    cfg = load_config()
    print("✅ Конфиг прочитан:")
    print(f"  • API_ID:               {cfg.api_id}")
    print(f"  • SESSION_NAMES:        {cfg.session_names}")
    print(f"  • ADMIN_IDS:            {sorted(cfg.admin_ids) or '—'}")
    print(f"  • BETA_MODE:            {cfg.beta_mode}  "
          f"(приглашено: {len(cfg.allowed_user_ids)})")
    print(f"  • USAGE_LIMIT:          {cfg.usage_limit}")
    print(f"  • DATA_DIR:             {cfg.data_dir}")
    print(f"  • STORAGE_ENABLED:      {cfg.storage_enabled}")
    print(f"  • FOUND_HISTORY_DAYS:   {cfg.found_history_days}")
    print(f"  • USE_EXTERNAL_DICT:    {cfg.use_external_dict}")
    print(f"  • SIMILAR_MAX_RESULTS:  {cfg.similar_max_results}")
    print(f"  • FILTER_MIN_SCORE:     {cfg.filter_min_score}")
    print(f"  • WATCH_INTERVALS:      "
          f"hot={cfg.watch_hot_interval}s, "
          f"warm={cfg.watch_warm_interval}s, "
          f"cold={cfg.watch_cold_interval}s")
    print(f"  • BACKUP_INCLUDE_SECRETS: {cfg.backup_include_secrets}")
    print(f"  • BANNER_PATH:          {cfg.banner_path or '— (текстовый баннер)'}")
    for w in cfg.runtime_warnings():
        print(f"  ⚠ {w}")
