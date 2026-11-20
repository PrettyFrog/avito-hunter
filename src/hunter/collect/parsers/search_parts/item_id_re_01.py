from __future__ import annotations

import re
from typing import Any, NamedTuple

from loguru import logger

ITEM_ID_RE = re.compile(r"(?:_i?|/)(\d{9,12})(?:\?|$|/)")
PRICE_RE = re.compile(r"(\d[\d\s\u00a0]{2,})")
INITIAL_RE = re.compile(
    r"(?:window\.__initialData__\s*=\s*|__NEXT_DATA__\"?\s*>?)\s*(\{.*)\s*;?\s*(?:</script>)?",
    re.S,
)

_CUTOFF_RE = re.compile(
    r"""(?xi)
    (?:
        class\s*=\s*["'][^"']*\bextraTitle\b[^"']*["']
        | data-marker\s*=\s*["'](?:similar-items|extra-items|recommendations)["']
        | >\s*(?:Похожие\s+объявления(?:\s+рядом|\s+в\s+других\s+городах)?|Объявления\s+в\s+других\s+городах|Рекомендации\s+для\s+вас)\s*<
    )
    """
)


class ParsedPrice(NamedTuple):
    price: int | None
    old_price: int | None = None
    discount_pct: float | None = None
    price_suspect: bool = False
    price_state: str = "missing"


class SearchHits(list):
    carousel_discarded: int = 0
    dom_hits: int = 0
    xhr_extra: int = 0
    accepted: int = 0
    xhr_extra_blobs: list = []
    total_count: int | None = None
    total_pages: int | None = None


def _extract_all_rub_tokens(text: str) -> list[int]:
    matches = re.findall(r"(\d[\d\s\u00a0]*)\s*(?:₽|руб)", text, re.IGNORECASE)
    tokens: list[int] = []
    for m in matches:
        digits = re.sub(r"[^\d]", "", m)
        if digits:
            tokens.append(int(digits))
    return tokens


def _extract_first_rub_token(text: str) -> int | None:
    tokens = _extract_all_rub_tokens(text)
    return tokens[0] if tokens else None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def _is_carousel_or_recommendation(node: Any) -> bool:
    curr = node
    for _ in range(6):
        if curr is None:
            break
        cls = (curr.attributes.get("class") or "").lower()
        marker = (curr.attributes.get("data-marker") or "").lower()
        if (
            "cvr-item" in cls
            or "cvr-items" in cls
            or "carousel" in cls
            or "carousel" in marker
            or "recommendation" in cls
            or "recommendation" in marker
            or "serp-banner" in marker
        ):
            return True
        curr = curr.parent
    return False


def _parse_price_html(node: Any) -> ParsedPrice:
    dn = node.css_first("[data-marker='discount-value'], [class*='discount']")
    dm = re.search(r"[-−–]?\s*(\d+(?:[.,]\d+)?)\s*%", dn.text()) if dn is not None else None
    discount_pct = float(dm.group(1).replace(",", ".")) if dm else None
    old_node = node.css_first("[data-marker='old-price-value'], [class*='old-price'], [class*='oldPrice'], del, s")
    old_price = (_extract_first_rub_token(old_node.text()) or _to_int(old_node.text())) if old_node else None

    attrs = getattr(node, "attributes", None) or {}
    meta = attrs.get("content") if attrs.get("itemprop") == "price" else None
    if meta is None and (el := node.css_first("meta[itemprop='price'][content], [itemprop='price'][content]")):
        meta = el.attributes.get("content")
    if meta is not None:
        c_val = _to_int(meta)
        if c_val == 0 or meta.strip() == "0":
            return ParsedPrice(None, old_price, discount_pct, False, "unspecified")
        price = c_val
    elif (marker := node.css_first("[data-marker='item-price-value']")) is not None:
        price = _extract_first_rub_token(marker.text()) or _to_int(marker.text())
    elif (ip := node.css_first("[itemprop='price']")) is not None:
        ip_c = ip.attributes.get("content")
        if ip_c is not None and (_to_int(ip_c) == 0 or ip_c.strip() == "0"):
            return ParsedPrice(None, old_price, discount_pct, False, "unspecified")
        price = _to_int(ip_c) or _extract_first_rub_token(ip.text())
    else:
        pn = node.css_first("[data-marker='item-price'], [class*='item-price'], [class*='price']")
        target = pn if pn is not None else node
        tt = target.text() if hasattr(target, "text") else ""
        price = _extract_first_rub_token(tt) or (_to_int(tt) if pn is not None else None)

    pn = node.css_first("[data-marker='item-price'], [class*='item-price'], [class*='price']")
    target = pn if pn is not None else node
    tt = target.text() if hasattr(target, "text") else ""
    if discount_pct is None and (dm := re.search(r"[-−–]?\s*(\d+(?:[.,]\d+)?)\s*%", tt)):
        discount_pct = float(dm.group(1).replace(",", "."))
    if old_price is None and len(tokens := _extract_all_rub_tokens(tt)) >= 2:
        old_price = tokens[1]
    if price is not None and price > 1_000_000:
        logger.warning("Price exceeds 1,000,000 (suspect): {}", price)
        return ParsedPrice(None, old_price, discount_pct, True, "suspect")
    if price is not None and price > 0:
        return ParsedPrice(price, old_price, discount_pct, False, "ok")
    if price is not None and price <= 0:
        return ParsedPrice(None, old_price, discount_pct, False, "unspecified")
    nt = node.text() if hasattr(node, "text") else ""
    pt = pn.text().lower() if pn and hasattr(pn, "text") else ""
    tl, nl = tt.lower(), nt.lower()
    if (
        any(k in tl or k in nl for k in ("цена не указана", "не указана", "договорная"))
        or "бесплатно" in pt
        or (pn is None and "бесплатно" in tl)
    ):
        return ParsedPrice(None, old_price, discount_pct, False, "unspecified")
    return ParsedPrice(None, old_price, discount_pct, False, "missing")
