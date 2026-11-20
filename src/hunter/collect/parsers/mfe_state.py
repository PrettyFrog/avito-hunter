from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from hunter.collect.parsers.mfe_state_parts.parse_mfe_state_01 import (
    _extract_catalog_items,
    _extract_item_url_and_id,
    _extract_mfe_meta,
    _extract_mfe_prices,
)
from hunter.collect.parsers.mfe_state_parts.parse_mfe_state_03 import (
    _build_mfe_extra,
    _extract_mfe_description,
    _extract_mfe_photos,
)
from hunter.core.models import SearchHit

MFE_STATE_TAG_RE = re.compile(
    r'<script[^>]*data-mfe-state=["\']true["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)

PREFERRED_IMAGE_RESOLUTIONS = (
    "1280x960",
    "636x636",
    "472x472",
    "416x416",
    "318x318",
    "236x236",
    "208x208",
)


def extract_mfe_state_json(html: str) -> dict[str, Any] | None:
    """Extracts raw JSON payload from <script data-mfe-state="true"> tag."""
    if "data-mfe-state" not in html:
        return None

    m = MFE_STATE_TAG_RE.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception as exc:
            logger.debug("Failed to json.loads regex-matched data-mfe-state: {}", exc)

    # Fallback to selectolax parser if regex missed due to unusual tag attributes
    try:
        from selectolax.lexbor import LexborHTMLParser as HTMLParser
    except ImportError:
        try:
            from selectolax.parser import HTMLParser
        except ImportError:
            return None

    try:
        tree = HTMLParser(html)
        tag = tree.css_first('script[data-mfe-state="true"]')
        if tag and tag.text():
            return json.loads(tag.text())
    except Exception as exc:
        logger.debug("Failed to extract data-mfe-state via HTMLParser: {}", exc)

    return None


def _best_photo_url(img_dict: dict[str, str]) -> str | None:
    if not isinstance(img_dict, dict) or not img_dict:
        return None
    for res in PREFERRED_IMAGE_RESOLUTIONS:
        if res in img_dict:
            return img_dict[res]
    # Fallback to first non-empty value
    for val in img_dict.values():
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None


def parse_mfe_state(
    html: str,
    *,
    base_url: str = "https://www.avito.ru",
) -> list[SearchHit]:
    """Parses search items from Avito data-mfe-state JSON blob.

    Returns empty list if tag is missing or contains no catalog items.
    """
    state = extract_mfe_state_json(html)
    items = _extract_catalog_items(state)
    if not items:
        return []

    hits: list[SearchHit] = []
    base_url_clean = base_url.rstrip("/")

    for item in items:
        if not isinstance(item, dict):
            continue

        extracted = _extract_item_url_and_id(item, base_url_clean)
        if extracted is None:
            continue
        ident, url = extracted

        title = item.get("title") or ""

        price, old_price, discount_pct = _extract_mfe_prices(item)
        city, seller_type, published_at = _extract_mfe_meta(item)

        photos, images_count = _extract_mfe_photos(item, _best_photo_url)
        desc, is_trunc = _extract_mfe_description(item)
        extra = _build_mfe_extra(item, photos, images_count, desc, is_trunc, published_at)

        hits.append(
            SearchHit(
                avito_id=ident,
                url=url,
                title=str(title) if title else None,
                price=price,
                old_price=old_price,
                discount_pct=discount_pct,
                price_suspect=False,
                price_state="active" if price is not None else "missing",
                city=str(city) if city else None,
                seller_type=seller_type,
                snippet=desc,
                published_at=published_at,
                extra=extra,
            )
        )

    return hits
