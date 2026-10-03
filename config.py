"""
Конфигурация приложения. Все параметры читаются из .env.

Правила:
  • load_dotenv() из python-dotenv — читает файл рядом.
  • Типы приводятся с валидацией, ошибки — понятные.
  • Если обязательное поле пусто — бот не стартует в «полу-рабочем» виде.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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


def _parse_int_set(raw: str) -> frozenset[int]:
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
            raise RuntimeError(f"❌ Ожидалось число, получено: {token!r}") from exc
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


def _get_banner_path() -> str:
    """
    BANNER_PATH — URL или локальный путь.
    URL возвращается как есть. Локальный путь нормализуется относительно
    корня проекта (папка, где лежит config.py).
    """
    raw = _get_str("BANNER_PATH")
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://")):
        return raw
    p = Path(raw)
    if not p.is_absolute():
        p = (Path(__file__).resolve().parent / p).resolve()
    return str(p)


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

    # Брендинг
    banner_path: str


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

    return Config(
        bot_token=bot_token,
        admin_ids=_parse_int_set(_get_str("ADMIN_IDS")),
        usage_limit=max(1, _get_int("USAGE_LIMIT", 3)),

        beta_mode=_get_bool("BETA_MODE", True),
        allowed_user_ids=_parse_int_set(_get_str("ALLOWED_USER_IDS")),

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

        use_external_dict=_get_bool("USE_EXTERNAL_DICT", True),
        similar_max_results=max(5, _get_int("SIMILAR_MAX_RESULTS", 20)),
        filter_min_score=max(1, min(10, _get_int("FILTER_MIN_SCORE", 3))),

        watch_hot_interval=hot,
        watch_warm_interval=warm,
        watch_cold_interval=cold,
        watch_batch_size=max(5, _get_int("WATCH_BATCH_SIZE", 30)),
        watch_sleep_after_days=max(1, _get_int("WATCH_SLEEP_AFTER_DAYS", 20)),

        banner_path=_get_banner_path(),
    )


if __name__ == "__main__":
    cfg = load_config()
    print("✅ Конфиг прочитан:")
    print(f"  • API_ID:               {cfg.api_id}")
    print(f"  • SESSION_NAMES:        {cfg.session_names}")
    print(f"  • ADMIN_IDS:            {sorted(cfg.admin_ids) or '—'}")
    print(f"  • USAGE_LIMIT:          {cfg.usage_limit}")
    print(f"  • STORAGE_ENABLED:      {cfg.storage_enabled}")
    print(f"  • FOUND_HISTORY_DAYS:   {cfg.found_history_days}")
    print(f"  • USE_EXTERNAL_DICT:    {cfg.use_external_dict}")
    print(f"  • SIMILAR_MAX_RESULTS:  {cfg.similar_max_results}")
    print(f"  • FILTER_MIN_SCORE:     {cfg.filter_min_score}")
    print(f"  • WATCH_INTERVALS:      "
          f"hot={cfg.watch_hot_interval}s, "
          f"warm={cfg.watch_warm_interval}s, "
          f"cold={cfg.watch_cold_interval}s")
    print(f"  • BANNER_PATH:          {cfg.banner_path or '— (текстовый баннер)'}")