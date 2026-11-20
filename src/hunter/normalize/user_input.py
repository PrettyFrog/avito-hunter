"""User input parsers and normalizers for non-programmer inputs (M3)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from hunter.notify.messages_ru import ADD_INVALID_PRICE, ADD_INVALID_URL

MAX_REALISTIC_PRICE = 10_000_000


def parse_price_input(text: str | None) -> int:
    """Parses user price input in various natural Russian formats:
    - "16000", "16 000", "16 000 ₽", "16 000р", "16 000 руб"
    - "16к", "16k", "16.5к", "16,5к"
    - "16 тыс", "16 тыс.", "16 тысяч", "16.5 тыс", "16,5 тысяч"

    Raises ValueError for non-numeric, zero, negative, or unrealistic (>10M) values.
    """
    if not text:
        raise ValueError(ADD_INVALID_PRICE)

    raw = text.strip().lower()
    # Replace non-breaking space and other unicode whitespace with standard space
    raw = re.sub(r"[\s\u00a0\u200b]+", " ", raw).strip()

    # Strip currency words and symbols from end
    raw = re.sub(r"[\s]*(?:₽|руб(?:лей|ля|\.)?|р\.?|rub)\s*$", "", raw).strip()

    # Check for 'тыс' multiplier (e.g. "16 тыс", "16 тыс.", "16.5 тыс", "16,5 тысяч")
    тыс_match = re.match(r"^([\d\s]+(?:[.,]\d+)?)\s*(?:тыс(?:яч[а-я]*)?\.?)$", raw)
    if тыс_match:
        num_str = тыс_match.group(1).replace(" ", "").replace(",", ".")
        try:
            val = float(num_str) * 1000
            price = int(round(val))
        except Exception as exc:
            raise ValueError(ADD_INVALID_PRICE) from exc
    else:
        # Check for 'к' / 'k' suffix (e.g. "16к", "16k", "16.5к", "16,5k")
        к_match = re.match(r"^([\d\s]+(?:[.,]\d+)?)\s*[kк]$", raw)
        if к_match:
            num_str = к_match.group(1).replace(" ", "").replace(",", ".")
            try:
                val = float(num_str) * 1000
                price = int(round(val))
            except Exception as exc:
                raise ValueError(ADD_INVALID_PRICE) from exc
        else:
            # Plain digits with optional spaces (e.g. "16 000", "16000")
            cleaned_digits = raw.replace(" ", "")
            if not re.match(r"^\d+$", cleaned_digits):
                raise ValueError(ADD_INVALID_PRICE)
            try:
                price = int(cleaned_digits)
            except Exception as exc:
                raise ValueError(ADD_INVALID_PRICE) from exc

    if price <= 0 or price > MAX_REALISTIC_PRICE:
        raise ValueError(ADD_INVALID_PRICE)

    return price


def extract_avito_search_url(text: str | None) -> tuple[str | None, str | None]:
    """Extracts and verifies an Avito search URL from message text.
    - Extracts URL even if surrounded by extra text words.
    - Rejects foreign domains with a clear refusal.
    - Rejects short links (e.g. avito.ru/s/...) with instruction to copy full browser URL.
    - Returns (clean_url, None) on success or (None, error_msg) on failure.
    """
    if not text:
        return None, ADD_INVALID_URL

    match = re.search(r"https?://[^\s\"'<>]+", text)
    if not match:
        return None, ADD_INVALID_URL

    url = match.group(0).rstrip(".,);]")
    parsed = urlparse(url)
    netloc = (parsed.netloc or "").lower()

    # Allowed Avito domains
    allowed = ("avito.ru", "www.avito.ru", "m.avito.ru")
    if not (netloc in allowed or netloc.endswith(".avito.ru")):
        return None, ADD_INVALID_URL

    # Short links check
    path = parsed.path.strip("/")
    if path.startswith("s/") or path == "s" or "share" in path:
        return (
            None,
            "Вы прислали короткую ссылку. Пожалуйста, откройте "
            "её в браузере и скопируйте полную ссылку из "
            "адресной строки.",
        )

    # Disallow direct single listing URLs (e.g.
    # /moskva/audio_i_video/naushniki_apple_airpods_max_4829104812
    # or /item/4829104812)
    if (
        re.search(r"_\d{8,12}$", path)
        or re.search(r"(?:^|/)item/\d{8,12}", path)
        or re.search(r"/\d{9,12}$", path)
    ):
        return (
            None,
            "Вы указали ссылку на конкретное объявление, а не "
            "на страницу поиска. Пожалуйста, выполните поиск на"
            " Авито и скопируйте ссылку со списком объявлений "
            "(из адресной строки поиска).",
        )

    return url, None
