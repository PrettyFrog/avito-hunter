from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002700-\U000027bf"
    "\U0001f1e0-\U0001f1ff"
    "]+",
    flags=re.UNICODE,
)
_SPACE_RE = re.compile(r"\s+")
_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


_BRAND_MAP = {
    "сони": "sony",
    "эппл": "apple",
    "эйпл": "apple",
    "синкпад": "thinkpad",
    "тинкпад": "thinkpad",
    "макбук": "macbook",
    "аирподс": "airpods",
    "аирподсы": "airpods",
    "эирподс": "airpods",
    "эирподсы": "airpods",
}


# Unicode dashes / hyphens
_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d]")
# Unicode spaces
_SPACE_CHARS_RE = re.compile(r"[\u00a0\u2000-\u200b\u202f\u205f\u3000]")
# Everything that is not a word char, whitespace, +, -, . or /
_CLEAN_RE = re.compile(r"[^\w\s+\-./]", flags=re.UNICODE)

# Brand substitutions precompiled once at import time. Compiling these inside the
# loop cost ~10k redundant re.compile() calls per search page.
_BRAND_RES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{re.escape(ru)}\b", flags=re.IGNORECASE), en)
    for ru, en in _BRAND_MAP.items()
)


_HOMOGLYPH_CYR_TO_LAT: dict[str, str] = {
    "а": "a",
    "в": "b",
    "с": "c",
    "е": "e",
    "к": "k",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "т": "t",
    "у": "y",
    "х": "x",
}


def _normalize_homoglyphs(word: str) -> str:
    """Normalize mixed Latin/Cyrillic homoglyphs or alphanumeric codes (e.g. a6000, m2)."""
    has_cyr_homo = any(c in _HOMOGLYPH_CYR_TO_LAT for c in word)
    if not has_cyr_homo:
        return word
    has_latin = any("a" <= c <= "z" for c in word)
    if has_latin:
        return "".join(_HOMOGLYPH_CYR_TO_LAT.get(c, c) for c in word)
    has_digits = any(c.isdigit() for c in word)
    if has_digits and all(c.isdigit() or c in _HOMOGLYPH_CYR_TO_LAT or "a" <= c <= "z" for c in word):
        return "".join(_HOMOGLYPH_CYR_TO_LAT.get(c, c) for c in word)
    return word


def normalize_text(value: str | None) -> str:
    """Normalize a title/query for matching. Cached: identical inputs are free."""
    if not value:
        return ""
    return _normalize_cached(str(value))


@lru_cache(maxsize=8192)
def _normalize_cached(text: str) -> str:
    # Unicode NFKC normalization and lowercasing
    text = unicodedata.normalize("NFKC", text).lower()
    # Replace ё with е
    text = text.replace("ё", "е")
    # Normalize dashes and spaces
    text = _DASH_RE.sub("-", text)
    text = _SPACE_CHARS_RE.sub(" ", text)
    # Remove emojis
    text = _EMOJI_RE.sub(" ", text)
    # Brand transliteration / synonyms
    for pattern, en in _BRAND_RES:
        text = pattern.sub(en, text)
    # Underscores to spaces, keep alphanumeric, spaces, +, -, ., /
    text = text.replace("_", " ")
    text = _CLEAN_RE.sub(" ", text)
    cleaned = _SPACE_RE.sub(" ", text).strip()
    if not cleaned:
        return ""
    words = [_normalize_homoglyphs(w) for w in cleaned.split(" ")]
    return " ".join(words)

