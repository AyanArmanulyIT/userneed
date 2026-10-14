"""
Генератор красивых кратких username'ов.

Пять стратегий:
  1. Слоговой    — vitoz, kamor, lupin
  2. Словарный   — storm, frame, pixel  (+ личные имена)
  3. Брендовый   — zenix, luxup, neohq
  4. Служебный   — v_max, x_777, a_one
  5. Случайный   — kxvzq, mwtbd

Ускорение:
  • Приоритет редких букв (q, z, x, j, w) — там плотность свободных
    выше в 5-10 раз, чем у имён на частые буквы.
  • Стоп-слова (assets/stopwords.txt) — исключаем the/and/for до проверки.
  • Для генерации используются ТОЛЬКО 5-буквенные слова/имена: раньше
    в пул попадали слова на 6-7 букв, которые потом отбрасывал фильтр
    длины — три четверти словаря тратились впустую.

Словари (assets/):
  words.txt, words_5.txt — английские слова (5-7 букв / только 5);
  names.txt              — личные имена (5-8 букв);
  stopwords.txt          — слова через пробел/запятую/перенос строки.
"""
from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable
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
# Частые (lmnrst) — вес 2, редкие (xzjkwv) — вес 5.
_FINALS = "lmnrst" * 2 + "xzjkwv" * 5

# Скрэмбл: расширен за счёт q, w, x, y — они самые «свободные».
_SCRAMBLE = "bcdfghjklmnprstvwxyz"
_RARE_FIRST = "qzxjwv"

# Длина имени, под которую заточены стратегии.
DEFAULT_LENGTH = 5


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
_BUILTIN_SET: frozenset[str] = frozenset(_BUILTIN_WORDS)


# =============================================================================
#                    Загрузка внешних словарей
# =============================================================================

_BASE_DIR = Path(__file__).resolve().parent.parent
_ASSETS = _BASE_DIR / "assets"


def _iter_tokens(path: Path) -> Iterable[str]:
    """Токены файла: разделители — перенос строки, пробел, запятая, ';'."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return []
    return re.split(r"[\s,;]+", text.lower())


def _read_words(path: Path, lo: int, hi: int) -> list[str]:
    """Читает словарь, фильтрует по длине и «только латиница», дедуплицирует."""
    out: list[str] = []
    for w in _iter_tokens(path):
        w = w.strip()
        if lo <= len(w) <= hi and w.isascii() and w.isalpha():
            out.append(w)
    return list(dict.fromkeys(out))


# Для скоринга — все длины; для генерации — только DEFAULT_LENGTH.
_EXT_WORDS: list[str] = []
_EXT_NAMES: list[str] = []
_EXT_WORDS_SET: frozenset[str] = frozenset()
_EXT_NAMES_SET: frozenset[str] = frozenset()
_STOPWORDS: frozenset[str] = frozenset()

_POOL_WORDS_5: tuple[str, ...] = ()
_POOL_NAMES_5: tuple[str, ...] = ()
_POOL_DICT_5: tuple[str, ...] = ()


def reload_external_dicts(use_external: bool = True) -> None:
    """
    Перечитывает словари.

    Стоп-слова загружаются ВСЕГДА (если файл есть) — они нужны и для
    встроенного словаря. use_external=False отключает только words/names.
    """
    global _EXT_WORDS, _EXT_NAMES, _EXT_WORDS_SET, _EXT_NAMES_SET, _STOPWORDS
    global _POOL_WORDS_5, _POOL_NAMES_5, _POOL_DICT_5

    _STOPWORDS = frozenset(_read_words(_ASSETS / "stopwords.txt", 1, 32))

    if use_external:
        raw_words = _read_words(_ASSETS / "words.txt", 5, 8)
        raw_words += _read_words(_ASSETS / "words_5.txt", 5, 5)
        raw_names = _read_words(_ASSETS / "names.txt", 5, 8)
        _EXT_WORDS = [w for w in dict.fromkeys(raw_words) if w not in _STOPWORDS]
        _EXT_NAMES = [w for w in dict.fromkeys(raw_names) if w not in _STOPWORDS]
    else:
        _EXT_WORDS, _EXT_NAMES = [], []

    _EXT_WORDS_SET = frozenset(_EXT_WORDS)
    _EXT_NAMES_SET = frozenset(_EXT_NAMES)

    builtin_5 = [w for w in _BUILTIN_WORDS if w not in _STOPWORDS]
    ext_5 = [w for w in _EXT_WORDS if len(w) == DEFAULT_LENGTH]
    _POOL_WORDS_5 = tuple(dict.fromkeys(builtin_5 + ext_5))
    _POOL_NAMES_5 = tuple(w for w in _EXT_NAMES if len(w) == DEFAULT_LENGTH)
    _POOL_DICT_5 = _POOL_WORDS_5 + _POOL_NAMES_5


def _is_word(name: str) -> bool:
    return name in _BUILTIN_SET or name in _EXT_WORDS_SET


def _is_name(name: str) -> bool:
    return name in _EXT_NAMES_SET


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
    return random.choice(_POOL_DICT_5) if _POOL_DICT_5 else _gen_syllabic()


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
    first = random.choice(_RARE_FIRST) if random.random() < 0.6 else random.choice(_SCRAMBLE)
    rest = "".join(
        random.choice(_SCRAMBLE + (VOWELS if random.random() < 0.3 else ""))
        for _ in range(DEFAULT_LENGTH - 1)
    )
    return first + rest


def _gen_scramble_mixed() -> str:
    return _gen_scramble()[:DEFAULT_LENGTH - 1] + random.choice(DIGITS)


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


def _gen_smart() -> str:
    """Случайная стратегия из микса."""
    return _pick_generator()()


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

    if _is_word(name):
        score += 3
        reason = "Осмысленное английское слово"
    elif _is_name(name):
        score += 2
        reason = "Личное имя"
    elif _SYLLABIC_RE.match(name):
        score += 1
        reason = "Читаемое псевдослово"
    elif "_" in name and name.replace("_", "").isalnum():
        word_part = name.split("_", 1)[1]
        if word_part in _SHORT_WORDS or word_part.isdigit():
            score += 1
            reason = "Служебный формат"
        else:
            reason = "Формат с разделителем"
    elif _RE_DIGIT.search(name):
        score -= 1
        reason = "Содержит цифры"
    elif _RE_CONS4.search(name):
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


def generate_username(length: int = DEFAULT_LENGTH) -> str:
    if length != DEFAULT_LENGTH:
        return _fallback(length)
    for _ in range(30):
        name = _gen_smart()
        if _is_clean(name, length) and name not in _STOPWORDS:
            return name
    return _fallback(length)


def generate_many(
    count: int,
    length: int = DEFAULT_LENGTH,
    exclude: set[str] | None = None,
) -> list[str]:
    """Осмысленные имена. exclude — что не предлагать (уже проверенные)."""
    if length != DEFAULT_LENGTH:
        return _generate_batch(count, length, lambda: _fallback(length), exclude)
    return _generate_batch(count, length, _gen_smart, exclude)


def generate_many_random(
    count: int,
    length: int = DEFAULT_LENGTH,
    exclude: set[str] | None = None,
) -> list[str]:
    """Случайные сочетания. exclude — что не предлагать."""
    if length != DEFAULT_LENGTH:
        return _generate_batch(count, length, lambda: _fallback(length), exclude)

    def strategy() -> str:
        return _gen_scramble_mixed() if random.random() < 0.15 else _gen_scramble()

    return _generate_batch(count, length, strategy, exclude)


def _generate_batch(
    count: int,
    length: int,
    strategy: Callable[[], str],
    exclude: set[str] | None = None,
) -> list[str]:
    if count <= 0:
        return []
    skip = exclude or set()
    result: list[str] = []
    seen: set[str] = set()
    attempts = 0
    cap = count * 60
    while len(result) < count and attempts < cap:
        attempts += 1
        name = strategy()
        if name in seen or name in skip:
            continue
        if not _is_clean(name, length) or name in _STOPWORDS:
            continue
        seen.add(name)
        result.append(name)
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
_TAIL_PAIRS = (
    "ix", "ex", "ax", "ox", "us", "io", "on", "an", "en", "er", "ly",
    "zz", "xx", "ox", "os", "is", "ik", "ek", "ak", "ov", "ev",
)
_HEAD_LETTERS = "ixoeuz"


def _t_substitute(base: str) -> set[str]:
    out: set[str] = set()
    for i, ch in enumerate(base):
        alt = _SIMILAR_MAP.get(ch)
        if alt:
            out.add(base[:i] + alt + base[i + 1:])
    return out


def _t_swap(base: str) -> set[str]:
    return {
        base[:i] + base[i + 1] + base[i] + base[i + 2:]
        for i in range(len(base) - 1)
    }


def _t_last(base: str) -> set[str]:
    return {base[:-1] + ch for ch in _POPULAR_TAILS if ch != base[-1]}


def _t_first(base: str) -> set[str]:
    out: set[str] = set()
    idx = LETTERS.find(base[0])
    if idx > 0:
        out.add(LETTERS[idx - 1] + base[1:])
    if idx < len(LETTERS) - 1:
        out.add(LETTERS[idx + 1] + base[1:])
    return out


def _t_vowel_replace(base: str) -> set[str]:
    out: set[str] = set()
    for i in range(1, len(base) - 1):
        if base[i] in VOWELS:
            for v in VOWELS:
                if v != base[i]:
                    out.add(base[:i] + v + base[i + 1:])
    return out


def _t_shorten(base: str) -> set[str]:
    """Убираем по одной гласной (кроме первой буквы)."""
    return {
        base[:i] + base[i + 1:]
        for i, ch in enumerate(base)
        if i > 0 and ch in VOWELS
    }


def _t_grow_one(base: str) -> set[str]:
    """Все варианты длиннее на один символ: вставка гласной, удвоение, хвост, голова."""
    out: set[str] = set()
    for i in range(1, len(base) + 1):
        for v in VOWELS:
            out.add(base[:i] + v + base[i:])
    for i, ch in enumerate(base):
        if ch.isalpha():
            out.add(base[: i + 1] + ch + base[i + 1:])        # удвоение буквы
    for ch in _POPULAR_TAILS:
        out.add(base + ch)
    for ch in _HEAD_LETTERS:
        out.add(ch + base)
    return out


def _t_grow_two(base: str) -> set[str]:
    return {base + pair for pair in _TAIL_PAIRS}


def _same_length_variants(base: str) -> set[str]:
    out: set[str] = set()
    for fn in (_t_substitute, _t_swap, _t_last, _t_first, _t_vowel_replace):
        out.update(fn(base))
    return out


def generate_similar(
    base: str,
    count: int = 20,
    length: int | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    """
    Детерминированный список вариантов, похожих на base.

    exclude — имена, которые уже проверялись: их не возвращаем, поэтому
    повторные вызовы с растущим exclude выдают НОВЫЕ кандидаты, а когда
    варианты закончатся — пустой список (вызывающий код должен это
    обрабатывать, а не крутиться в цикле).
    """
    base = base.lstrip("@").strip().lower()
    if not base or not (base.isascii() and base.isalpha()):
        return []

    if length is None:
        length = DEFAULT_LENGTH if len(base) <= DEFAULT_LENGTH else len(base)
    skip = set(exclude or ())

    # Уровень 1: приводим к нужной длине.
    level1: set[str]
    if len(base) == length:
        level1 = _same_length_variants(base)
    elif len(base) < length:
        grown = {base}
        for _ in range(length - len(base)):
            step: set[str] = set()
            for g in grown:
                if len(g) >= length:
                    step.add(g)
                    continue
                step.update(_t_grow_one(g))
                if length - len(g) == 2:
                    step.update(_t_grow_two(g))
            grown = step
        level1 = {g for g in grown if len(g) == length}
    else:
        shrunk = {base}
        for _ in range(len(base) - length):
            shrunk = {x for s in shrunk for x in _t_shorten(s)}
        level1 = {s for s in shrunk if len(s) == length}

    def _clean(cands: Iterable[str]) -> list[str]:
        return [
            c for c in cands
            if c != base and c not in skip and c.isalpha()
            and _is_clean(c, length) and c not in _STOPWORDS
        ]

    candidates = set(_clean(level1))

    # Уровень 2: если мало — варьируем уже полученные (соседние формы).
    if len(candidates) < count:
        level2: set[str] = set()
        for c in list(level1)[:200]:
            if len(c) == length:
                level2.update(_same_length_variants(c))
        candidates.update(_clean(level2))

    def _prefix_match(x: str) -> int:
        n = 0
        for a, b in zip(x, base):
            if a != b:
                break
            n += 1
        return n

    ordered = sorted(
        candidates,
        key=lambda x: (
            0 if _is_word(x) else (1 if _is_name(x) else 2),
            -_prefix_match(x),
            x,
        ),
    )
    return ordered[: max(1, count)]


# =============================================================================
#                              Утилиты
# =============================================================================

def dictionary_size() -> tuple[int, int]:
    """(слов для генерации, имён для генерации) — только 5-буквенные."""
    return len(_POOL_WORDS_5), len(_POOL_NAMES_5)


def stopwords_count() -> int:
    return len(_STOPWORDS)


if __name__ == "__main__":
    reload_external_dicts()
    w, n = dictionary_size()
    print(f"Пул генерации: {w} слов, {n} имён; стоп-слов: {stopwords_count()}\n")

    print("=== Смешанный (примеры) ===")
    for i, name in enumerate(generate_many(15), 1):
        sc, why = score_username(name)
        print(f"  {i:2}. {name:8}  {sc}/10  ·  {why}")

    print("\n=== Скрамбл (примеры) ===")
    for i, name in enumerate(generate_many_random(8), 1):
        sc, why = score_username(name)
        print(f"  {i}. {name:8}  {sc}/10  ·  {why}")

    print("\n=== Похожие ===")
    for b in ("abc", "alex", "vital", "vitaly"):
        print(f"  {b:8} → {generate_similar(b, count=10)}")
