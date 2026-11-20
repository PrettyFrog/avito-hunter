from __future__ import annotations

import re
from typing import Any

from loguru import logger

from hunter.collect.parsers.search_parts.item_id_re_01 import (
    ParsedPrice,
    _extract_all_rub_tokens,
    _extract_first_rub_token,
)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def _parse_price_numeric(node: int | float) -> ParsedPrice:
    val = int(node)
    if val <= 0:
        return ParsedPrice(None, None, None, False, "unspecified")
    if val > 1_000_000:
        logger.warning("Price exceeds 1,000,000 (suspect): {}", val)
        return ParsedPrice(None, None, None, True, "suspect")
    return ParsedPrice(val, None, None, False, "ok")


def _parse_price_str(node: str) -> ParsedPrice:
    text, text_lower = node.strip(), node.strip().lower()
    if any(k in text_lower for k in ("цена не указана", "не указана")) or text_lower in (
        "0", "0 ₽", "0 руб", "0руб", "бесплатно", "даром"
    ):
        return ParsedPrice(None, None, None, False, "unspecified")
    dm = re.search(r"[-−–]?\s*(\d+(?:[.,]\d+)?)\s*%", text)
    discount_pct = float(dm.group(1).replace(",", ".")) if dm else None
    tokens = _extract_all_rub_tokens(text)
    price, old_price = (
        (tokens[0], tokens[1] if len(tokens) > 1 else None)
        if tokens
        else (_to_int(text), None)
    )
    if price is not None and price <= 0:
        return ParsedPrice(None, old_price, discount_pct, False, "unspecified")
    if price is not None and price > 1_000_000:
        logger.warning("Price exceeds 1,000,000 (suspect): {}", price)
        return ParsedPrice(None, old_price, discount_pct, True, "suspect")
    if price is not None and price > 0:
        return ParsedPrice(price, old_price, discount_pct, False, "ok")
    if "договорная" in text_lower:
        return ParsedPrice(None, old_price, discount_pct, False, "unspecified")
    return ParsedPrice(None, old_price, discount_pct, False, "missing")


def _extract_dom_price(
    node: Any, old_price: int | None, discount_pct: float | None
) -> tuple[int | None, ParsedPrice | None]:
    attrs = getattr(node, "attributes", None) or {}
    meta_content = attrs.get("content") if attrs.get("itemprop") == "price" else None
    if meta_content is None and (el := node.css_first(
        "meta[itemprop='price'][content], "
        "[itemprop='price'][content]")):
        meta_content = el.attributes.get("content")
    if meta_content is not None:
        c_val = _to_int(meta_content)
        if c_val == 0 or meta_content.strip() == "0":
            return None, ParsedPrice(None, old_price, discount_pct, False, "unspecified")
        return c_val, None
    if (marker := node.css_first("[data-marker='item-price-value']")) is not None:
        return _extract_first_rub_token(marker.text()) or _to_int(marker.text()), None
    if (ip := node.css_first("[itemprop='price']")) is not None:
        ip_c = ip.attributes.get("content")
        if ip_c is not None and (_to_int(ip_c) == 0 or ip_c.strip() == "0"):
            return None, ParsedPrice(None, old_price, discount_pct, False, "unspecified")
        return _to_int(ip_c) or _extract_first_rub_token(ip.text()), None
    pn = node.css_first("[data-marker='item-price'], [class*='item-price'], [class*='price']")
    target = pn if pn is not None else node
    tt = target.text() if hasattr(target, "text") else ""
    return _extract_first_rub_token(tt) or (_to_int(tt) if pn is not None else None), None
