"""Avito URL normalization and f-parameter price filter updating."""

from __future__ import annotations

import base64
import json
import math
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from loguru import logger

PROHIBITED_CATEGORIES: dict[str, str] = {
    "nedvizhimost": "недвижимость",
    "kvartiry": "недвижимость (квартиры)",
    "doma_dachi_kottedzhi": "недвижимость (дома)",
    "kommercheskaya_nedvizhimost": "коммерческая недвижимость",
    "avtomobili": "автомобили",
    "transport": "транспорт",
    "mototsikly": "мототехника",
    "gruzoviki_i_spetstehnika": "спецтехника",
    "vakansii": "вакансии",
    "rabota": "работа",
    "rezyme": "резюме",
    "uslugi": "услуги",
    "predlozheniya_uslug": "услуги",
}


def _check_prohibited_category(path: str) -> str | None:
    for seg, cat_name in PROHIBITED_CATEGORIES.items():
        if f"/{seg}" in path:
            return f"Ошибка: категория '{cat_name}' находится вне охвата бота (только потребительские товары)."
    return None


def _apply_price_bounds(data: dict[str, Any], new_to: int, new_from: int | None) -> None:
    data["to"] = new_to
    if new_from is not None and new_from > 0:
        data["from"] = new_from
    else:
        data.pop("from", None)


def _update_avito_f_param(f_val: str, new_to: int, new_from: int | None = None) -> str:
    """Updates price filter inside Avito base64-encoded 'f' parameter."""
    try:
        padded = f_val + "=" * ((4 - len(f_val) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded)
        idx = raw.find(b'{"')
        if idx == -1:
            return f_val

        if idx == 0:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                return f_val
            _apply_price_bounds(data, new_to, new_from)
            new_json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(new_json_bytes).decode("ascii").rstrip("=")

        varint_start = idx - 1
        if raw[varint_start] & 0x80 != 0:
            return f_val
        while varint_start > 0 and (raw[varint_start - 1] & 0x80) != 0:
            varint_start -= 1

        from hunter.domain.query_url import _decode_varint, _encode_varint

        old_len, end_pos = _decode_varint(raw, varint_start)
        if end_pos != idx:
            return f_val

        json_bytes = raw[idx : idx + old_len]
        data = json.loads(json_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return f_val
        _apply_price_bounds(data, new_to, new_from)
        new_json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
        new_raw = (
            raw[:varint_start]
            + _encode_varint(len(new_json_bytes))
            + new_json_bytes
            + raw[idx + old_len :]
        )
        return base64.urlsafe_b64encode(new_raw).decode("ascii").rstrip("=")
    except Exception as exc:
        logger.debug("Failed to update f parameter price filter: {}", exc)
        return f_val


def normalize_avito_url(
    url: str,
    max_price: int,
    headroom_pct: float = 0.20,
    min_price: int | None = None,
) -> tuple[str, str | None]:
    """Validates and normalizes Avito search URL."""
    p = urlparse(url.strip())
    netloc = p.netloc.lower()
    if "avito.ru" not in netloc:
        return "", "Ошибка: ссылка должна вести на домен avito.ru"

    if netloc == "m.avito.ru" or not netloc:
        netloc = "www.avito.ru"

    path = p.path
    err = _check_prohibited_category(path)
    if err:
        return "", err

    qs = parse_qs(p.query, keep_blank_values=False)
    strip_keys = {"p", "page", "context", "from", "presentationType"}
    clean_qs = {k: v for k, v in qs.items() if k not in strip_keys and not k.startswith("utm_")}

    clean_qs["s"] = ["104"]

    headroom_max = int(math.ceil(max_price * (1.0 + headroom_pct)))
    clean_qs["pmax"] = [str(headroom_max)]

    if min_price is not None and min_price > 0:
        clean_qs["pmin"] = [str(min_price)]
    else:
        clean_qs.pop("pmin", None)

    if "f" in clean_qs and clean_qs["f"]:
        updated_f = _update_avito_f_param(clean_qs["f"][0], headroom_max, min_price)
        clean_qs["f"] = [updated_f]

    new_query = urlencode(sorted(clean_qs.items()), doseq=True)
    return urlunparse((p.scheme or "https", netloc, path, "", new_query, "")), None
