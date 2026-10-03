"""
Генератор красивых кратких username'ов.

Пять стратегий:
  1. Слоговой    — vitoz, kamor, lupin
  2. Словарный   — storm, frame, pixel
  3. Брендовый   — zenix, luxup, neohq
  4. Служебный   — v_max, x_777, a_one
  5. Случайный   — kxvzq, mwtbd

Ускорение:
  • Приоритет редких букв (q, z, x, j, w) — там плотность свободных
    выше в 5-10 раз, чем у имён на частые буквы.
  • Стоп-слова (assets/stopwords.txt) — исключаем the/and/for до проверки.
"""
from __future__ import annotations

import random
import re
from collections.abc import Callable
from pathlib import Path

# =============================================================================
#                              Алфавиты
# =============================================================================

LETTERS = "abcdefghijklmnopqrstuvwxyz"
VOWELS = "aeiou"
DIGITS = "0123456789"

# Согласные для слогового генератора.
INITIALS = "bdfgjklmnprstvz"

# Финалы: распределение смещено в сторону редких букв.
# Частые (lm nrst) — вес 2, редкие (x z j k w v u) — вес 5.
# Это сдвигает выдачу к «свободным» именам, не ломая читаемости.
_FINALS = "lmnrst" * 2 + "xzjkwv" * 5

# Скрэмбл: расширен за счёт q, w, x, y — их раньше не было, а они самые
# «свободные». q без u выглядит странно, но для скрэмбла это ок.
_SCRAMBLE = "bcdfghjklmnprstvwxyz"


# =============================================================================
#                           Встроенный словарь
# =============================================================================

_BUILTIN_WORDS_RAW = """
storm frame pixel flame orbit candy honey cloud crown dance dream eagle
earth field flash flight flute focus force forest frost ghost glory grace
grain grand grape grass green happy heart honor house joker juice knife
laugh light lucky magic major maple march medal moons mount music night
north ocean onion opera paint paper peace pearl piano pilot plant plaza
point pound power press pride prime prize proof pulse queen quick quiet
radar radio rally ranch raven reach realm relax rider river robin robot
rocky rough royal saint salad sauce scale scene score sense shade shape
share sharp sheep sheet shelf shell shift shine shirt shock shore short
sigma silly since sight skill slick slope smart smile smoke solid sound
south space spark speak speed spice spike spine split spoon sport squad
stack staff stage stake stamp stand steam steel stick still stock stone
store story stove style sugar suite sunny super sweet swift sword table
taste teach tenor terms theme there thick thing think third three throw
tiger tight timer tired title today toast trace track trade trail train
trait trash trend trial tribe trick tried truck truly trust truth twice
twist ultra uncle union unite unity until upper upset urban usage usual
valid value vapor vault vegan venue verse video villa vinyl viral virus
visit vital vivid vocal voice voter wagon waste watch water weigh weird
whale wheat wheel where which while white whole whose woman world worry
worse worst worth would wound write wrong wrote yield young youth zebra
civil clear click climb clock close cloth coach coast color comet comic
coral count court cover crack craft crash crazy cream crime cross crowd
curve cycle daily dairy dated dealt death debut delay depth doing doubt
dozen draft drain drama drawn dread dried drink drive drunk dying eager
early eight elder elect elite empty enemy enjoy enter entry equal error
event every exact exile exist extra faith false fatal fault fiber fifth
fifty fight final first fixed fleet floor fluid
""".split()

_BUILTIN_WORDS: tuple[str, ...] = tuple(dict.fromkeys(_BUILTIN_WORDS_RAW))


# =============================================================================
#                    Загрузка внешних словарей
# =============================================================================

_BASE_DIR = Path(__file__).resolve().parent.parent
_ASSETS = _BASE_DIR / "assets"


def _read_lines(path: Path, lo: int, hi: int, alpha_only: bool = True) -> list[str]:
    """Читает файл построчно, фильтрует по длине, дедуплицирует."""
    if not path.is_file():
        return []
    out: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            w = line.strip().lower()
            if not (lo <= len(w) <= hi):
                continue
            if alpha_only and not w.isalpha():
                continue
            out.append(w)
    except Exception:
        return []
    return list(dict.fromkeys(out))


_EXT_WORDS: list[str] = []
_EXT_NAMES: list[str] = []
_STOPWORDS: set[str] = set()


def reload_external_dicts(use_external: bool = True) -> None:
    """
    Перечитывает внешние словари. Стоп-слова загружаются всегда,
    если файл есть — они отсекаются из основных пулов.
    """
    global _EXT_WORDS, _EXT_NAMES, _STOPWORDS

    if not use_external:
        _EXT_WORDS, _EXT_NAMES = [], []
        _STOPWORDS = set()
        return

    _STOPWORDS = set(_read_lines(_ASSETS / "stopwords.txt", 1, 20))

    raw_words = _read_lines(_ASSETS / "words.txt", 5, 7)
    raw_names = _read_lines(_ASSETS / "names.txt", 5, 8)

    # Убираем стоп-слова из обоих пулов.
    _EXT_WORDS = [w for w in raw_words if w not in _STOPWORDS]
    _EXT_NAMES = [w for w in raw_names if w not in _STOPWORDS]


def _words_pool() -> list[str]:
    return [w for w in _BUILTIN_WORDS if w not in _STOPWORDS] + _EXT_WORDS


def _names_pool() -> list[str]:
    return _EXT_NAMES


# =============================================================================
#                          Фильтр «мусора»
# =============================================================================

_RE_REPEAT3 = re.compile(r"(.)\1\1")
_RE_CONS4 = re.compile(r"[bcdfghjklmnpqrstvwxz]{4,}")
_RE_VOW3 = re.compile(r"[aeiouy]{3,}")
_RE_DIGIT = re.compile(r"\d")


def _is_clean(name: str, length: int) -> bool:
    if len(name) != length:
        return False
    if _RE_REPEAT3.search(name):
        return False
    if _RE_CONS4.search(name):
        return False
    if _RE_VOW3.search(name):
        return False
    return True


# =============================================================================
#                               Стратегии
# =============================================================================

def _gen_syllabic() -> str:
    """CV + CVC. Финалы смещены в редкие буквы через _FINALS."""
    head = random.choice(INITIALS) + random.choice(VOWELS)
    tail = random.choice(INITIALS) + random.choice(VOWELS) + random.choice(_FINALS)
    return head + tail


def _gen_dictionary() -> str:
    pool = _words_pool() + _names_pool()
    return random.choice(pool) if pool else _gen_syllabic()


# Брендовые префиксы — добавлены редкие для расширения пула.
_PREFIX_3 = (
    "ace", "air", "arc", "bit", "dex", "fly", "fox", "hex", "ice",
    "ion", "key", "lex", "lux", "max", "neo", "one", "pro", "red",
    "rex", "sky", "sol", "sun", "top", "zen", "zip",
    # Редкие (более свободные):
    "vex", "wax", "qix", "zap", "jax", "vox", "zyx", "wyz",
)
_SUFFIX_2 = ("ax", "ex", "ix", "ox", "ux", "hq", "up", "io", "zo", "on")
_PREFIX_2 = (
    "bi", "di", "do", "go", "hi", "jo", "ki", "li", "ma", "mi",
    "mo", "na", "ra", "ta", "vi", "ze", "zo",
)
_SUFFIX_3 = (
    "ace", "bit", "box", "com", "dex", "fox", "hex", "hub",
    "ion", "lab", "lux", "max", "net", "neo", "pro", "sky", "zen",
)


def _gen_brand() -> str:
    if random.random() < 0.5:
        return random.choice(_PREFIX_3) + random.choice(_SUFFIX_2)
    return random.choice(_PREFIX_2) + random.choice(_SUFFIX_3)


_STATUS_LETTERS = "aefgijklmnopstuvxyz"
_SHORT_WORDS = (
    "ace", "air", "arc", "bit", "fox", "god", "ice", "ion",
    "joy", "key", "lab", "lux", "max", "neo", "net", "one",
    "pro", "sky", "top", "vip", "war", "win", "zen",
)
_COOL_NUMBERS = (
    "007", "069", "100", "111", "123", "321",
    "333", "555", "666", "777", "888", "999",
)


def _gen_status() -> str:
    first = random.choice(_STATUS_LETTERS)
    if random.random() < 0.6:
        return f"{first}_{random.choice(_SHORT_WORDS)}"
    return f"{first}_{random.choice(_COOL_NUMBERS)}"


def _gen_scramble() -> str:
    """
    Случайное сочетание. Первой ставим редкую букву (в 60% случаев) —
    это повышает шанс попадания в свободное имя.
    """
    # Rare-first: с вероятностью 60% первая буква — из редких.
    if random.random() < 0.6:
        first = random.choice("qzxjwv")
    else:
        first = random.choice(_SCRAMBLE)
    rest = "".join(
        random.choice(_SCRAMBLE + ("aeiou" if random.random() < 0.3 else ""))
        for _ in range(4)
    )
    return first + rest


def _gen_scramble_mixed() -> str:
    return _gen_scramble()[:4] + random.choice(DIGITS)


# =============================================================================
#                             Микс стратегий
# =============================================================================

_BOUNDS: tuple[tuple[float, Callable[[], str]], ...] = (
    (0.30, _gen_syllabic),
    (0.55, _gen_dictionary),
    (0.75, _gen_brand),
    (1.00, _gen_status),
)


def _pick_generator() -> Callable[[], str]:
    r = random.random()
    for bound, fn in _BOUNDS:
        if r < bound:
            return fn
    return _gen_syllabic


# =============================================================================
#                            Скоринг (1-10)
# =============================================================================

_SYLLABIC_RE = re.compile(
    r"^[bcdfghjklmnpqrstvwxz][aeiou]"
    r"[bcdfghjklmnpqrstvwxz][aeiou]"
    r"[bcdfghjklmnpqrstvwxz]$"
)


def _score_reason(name: str) -> tuple[int, str]:
    score = 5
    reason = "Стандартный формат"
    length = len(name)

    if length == 5:
        score += 1
    elif length in (6, 7):
        score += 0
    else:
        score -= 1

    if name in _BUILTIN_WORDS or name in _EXT_WORDS:
        score += 3
        reason = "Осмысленное английское слово"
    elif name in _EXT_NAMES:
        score += 2
        reason = "Личное имя"
    elif _SYLLABIC_RE.match(name):
        score += 1
        reason = "Читаемое псевдослово"
    elif "_" in name and name.replace("_", "").isalpha():
        word_part = name.split("_", 1)[1]
        if word_part in _SHORT_WORDS or word_part.isdigit():
            score += 1
            reason = "Служебный формат"
        else:
            reason = "Формат с разделителем"
    elif _RE_DIGIT.search(name):
        score -= 1
        reason = "Содержит цифры"
    elif _RE_CONS4.search(name[:1] + name):
        score -= 2
        reason = "Трудное для чтения"

    if "_" in name and length == 5 and not name.startswith(("x_", "v_", "i_")):
        score -= 1
    if name.count("_") > 1:
        score -= 1
    if _RE_REPEAT3.search(name):
        score -= 2
        reason = "Визуально повторяющееся"
    if not any(ch in VOWELS for ch in name):
        score -= 1
        reason = "Нет гласных"

    return max(1, min(10, score)), reason


def score_username(name: str) -> tuple[int, str]:
    name = name.lstrip("@").strip().lower()
    if not name:
        return 1, "Пустое имя"
    return _score_reason(name)


# =============================================================================
#                        Базовый генератор
# =============================================================================

def generate_username(length: int = 5) -> str:
    if length != 5:
        return _fallback(length)
    for _ in range(30):
        name = _pick_generator()()
        if _is_clean(name, length) and name not in _STOPWORDS:
            return name
    return _fallback(length)


def _fallback(length: int) -> str:
    if length < 2:
        return "".join(random.choice(LETTERS) for _ in range(max(1, length)))
    for _ in range(60):
        name = (
            random.choice(LETTERS)
            + "".join(random.choice(LETTERS + DIGITS) for _ in range(length - 2))
            + random.choice(LETTERS)
        )
        if _is_clean(name, length):
            return name
    return "".join(random.choice(LETTERS) for _ in range(length))

def _gen_smart() -> str:
    """Обёртка — вызывает случайную стратегию и возвращает готовое имя."""
    return _pick_generator()()

def generate_many(count: int, length: int = 5) -> list[str]:
    return _generate_batch(count, length, strategy=_gen_smart)


def generate_many_random(count: int, length: int = 5) -> list[str]:
    if length != 5:
        return _generate_batch(count, length, strategy=lambda: _fallback(length))
    return _generate_batch(
        count,
        length,
        strategy=lambda: _gen_scramble_mixed()
        if random.random() < 0.15
        else _gen_scramble(),
    )


def _generate_batch(
    count: int,
    length: int,
    strategy: Callable[[], str],
) -> list[str]:
    if count <= 0:
        return []
    result: list[str] = []
    seen: set[str] = set()
    cap = count * 40
    attempts = 0
    while len(result) < count and attempts < cap:
        for _ in range(max(10, (count - len(result)) * 2)):
            attempts += 1
            name = strategy()
            if name in seen or not _is_clean(name, length):
                continue
            if name in _STOPWORDS:
                continue
            seen.add(name)
            result.append(name)
            if len(result) >= count:
                break
    return result


# =============================================================================
#                          Похожие имена
# =============================================================================

_SIMILAR_MAP: dict[str, str] = {
    "a": "e", "e": "a", "i": "y", "y": "i", "o": "u", "u": "o",
    "c": "k", "k": "c", "s": "z", "z": "s", "f": "v", "v": "f",
    "b": "p", "p": "b", "d": "t", "t": "d", "m": "n", "n": "m",
    "g": "j", "j": "g",
}

_POPULAR_TAILS = "xyzlnrstmv"


def _transform_substitute(base: str) -> set[str]:
    out: set[str] = set()
    for i, ch in enumerate(base):
        alt = _SIMILAR_MAP.get(ch)
        if alt:
            out.add(base[:i] + alt + base[i + 1:])
    return out


def _transform_swap(base: str) -> set[str]:
    out: set[str] = set()
    for i in range(len(base) - 1):
        out.add(base[:i] + base[i + 1] + base[i] + base[i + 2:])
    return out


def _transform_last(base: str) -> set[str]:
    out: set[str] = set()
    for ch in _POPULAR_TAILS:
        if ch != base[-1]:
            out.add(base[:-1] + ch)
    return out


def _transform_first(base: str) -> set[str]:
    out: set[str] = set()
    first = base[0]
    idx = LETTERS.find(first)
    if idx > 0:
        out.add(LETTERS[idx - 1] + base[1:])
    if idx < len(LETTERS) - 1:
        out.add(LETTERS[idx + 1] + base[1:])
    return out


def _transform_vowel_inject(base: str) -> set[str]:
    out: set[str] = set()
    for i in range(1, len(base) - 1):
        if base[i] not in VOWELS:
            for v in VOWELS:
                out.add(base[:i] + v + base[i + 1:])
    return out


def _transform_shorten(base: str) -> set[str]:
    out: set[str] = set()
    for i, ch in enumerate(base):
        if ch in VOWELS:
            out.add(base[:i] + base[i + 1:])
    return out


def _transform_lengthen(base: str) -> set[str]:
    out: set[str] = set()
    for i in range(1, len(base)):
        out.add(base[:i] + random.choice(VOWELS) + base[i:])
    return out


def generate_similar(base: str, count: int = 20, length: int | None = None) -> list[str]:
    base = base.lstrip("@").strip().lower()
    if not base or not base.isalpha():
        return []

    if length is None:
        length = 5 if len(base) <= 5 else len(base)

    candidates: set[str] = set()
    for fn in (
        _transform_substitute, _transform_swap, _transform_last,
        _transform_first, _transform_vowel_inject,
    ):
        if len(base) == length:
            candidates.update(fn(base))

    if len(base) < length:
        candidates.update(_transform_lengthen(base))
        for x in list(candidates):
            if len(x) < length:
                candidates.add(x + random.choice(_POPULAR_TAILS))
    elif len(base) > length:
        candidates.update(_transform_shorten(base))

    clean = [
        c for c in candidates
        if c != base and _is_clean(c, length) and c.isalpha()
        and c not in _STOPWORDS
    ]
    clean.sort(key=lambda x: (
        0 if x in _BUILTIN_WORDS or x in _EXT_WORDS else 1,
        len(x),
    ))
    return clean[: max(1, count)]


# =============================================================================
#                              Утилиты
# =============================================================================

def dictionary_size() -> tuple[int, int]:
    return len(_words_pool()), len(_names_pool())


def stopwords_count() -> int:
    return len(_STOPWORDS)


if __name__ == "__main__":
    reload_external_dicts()
    w, n = dictionary_size()
    print(f"Словарь: {w} слов, {n} имён, {stopwords_count()} стоп-слов\n")

    print("=== Смешанный (примеры) ===")
    for i, name in enumerate(generate_many(15), 1):
        sc, why = score_username(name)
        print(f"  {i:2}. {name:8}  {sc}/10  ·  {why}")

    print("\n=== Скрамбл (примеры) ===")
    for i, name in enumerate(generate_many_random(8), 1):
        sc, why = score_username(name)
        print(f"  {i}. {name:8}  {sc}/10  ·  {why}")