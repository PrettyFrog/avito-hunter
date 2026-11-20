from __future__ import annotations

import json
import re
from typing import Any

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    from selectolax.parser import HTMLParser

from hunter.collect.parsers.card_parts.parse_card_mobile_json_01 import (
    _collect_raw_card_image_urls,
    _extract_card_city_and_delivery,
    _extract_card_prices,
    _extract_card_title,
    _filter_and_rank_card_images,
    _extract_mobile_params,
    _extract_mobile_photos,
    _extract_mobile_price,
    _extract_mobile_seller,
)
from hunter.collect.parsers.search import (
    _json_blobs,
    _walk_items,
    extract_avito_id,
)
from hunter.core.models import Card
from hunter.core.rawstore import scrub_text


def parse_card_mobile_json(data: dict[str, Any], *, url: str) -> Card | None:
    """Parses native mobile JSON payload from https://m.avito.ru/api/1/card/items/{id}."""
    m = data.get("success", {}).get("mobile", {})
    if not m:
        return None
    item_id = str(m.get("id") or extract_avito_id(url) or "")
    if not item_id:
        return None

    photos = _extract_mobile_photos(m)
    price_val = _extract_mobile_price(m)
    seller_id, seller_type = _extract_mobile_seller(m)
    params = _extract_mobile_params(m)

    return Card(
        avito_id=item_id,
        url=url or f"https://www.avito.ru/{item_id}",
        title=m.get("title") or "",
        price=price_val,
        description=m.get("description"),
        city=m.get("address"),
        seller_id=seller_id,
        seller_type=seller_type,
        image_urls=photos[:12],
        params=params,
    )


def parse_card(html: str, *, url: str) -> Card | None:
    trimmed = (html or "").strip()
    if trimmed.startswith("{") and ('"mobile"' in trimmed or '"success"' in trimmed):
        try:
            j_data = json.loads(trimmed)
            card = parse_card_mobile_json(j_data, url=url)
            if card:
                return card
        except Exception:
            pass
    html = scrub_text(html)
    avito_id = extract_avito_id(url)
    tree = HTMLParser(html)
    data: dict[str, Any] = {}
    for blob in _json_blobs(html):
        for item in _walk_items(blob):
            ident = str(item.get("id") or item.get("itemId") or "")
            if avito_id and ident and ident != avito_id:
                continue
            data = item
            if ident:
                avito_id = ident
            break
        if data:
            break
    if not avito_id:
        return None

    title = _extract_card_title(tree, data)
    description = data.get("description") or _text(
        tree, "[data-marker='item-view/item-description'], [itemprop='description']"
    )
    params = _params(tree, data)
    price, old_price, discount_pct, price_suspect = _extract_card_prices(tree, data)
    images = _images(tree, data)
    seller_id, seller_type = _seller(tree, data)
    city, delivery = _extract_card_city_and_delivery(tree, data)
    return Card(
        avito_id=str(avito_id),
        url=url,
        title=title,
        price=price,
        old_price=old_price,
        discount_pct=discount_pct,
        price_suspect=price_suspect,
        description=description,
        params=params,
        city=city,
        delivery=delivery,
        seller_id=seller_id,
        seller_type=seller_type,
        image_urls=images,
        published=str(data.get("sortTimeStamp") or data.get("time") or "") or None,
    )


def _text(tree: HTMLParser, selector: str) -> str | None:
    node = tree.css_first(selector)
    if node is None:
        return None
    text = node.text(separator="\n", strip=True)
    return text or None


def _params(tree: HTMLParser, data: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    raw = data.get("params") or data.get("characteristics") or []
    if isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and row.get("title") and row.get("description"):
                params[str(row["title"])] = str(row["description"])
            elif isinstance(row, dict) and row.get("name") and row.get("value"):
                params[str(row["name"])] = str(row["value"])
    elif isinstance(raw, dict):
        params = {str(k): str(v) for k, v in raw.items()}
    for li in tree.css("[data-marker='item-view/item-params'] li, ul[class*='params'] li"):
        text = li.text(separator=": ", strip=True)
        if ":" in text:
            key, value = text.split(":", 1)
            params.setdefault(key.strip(), value.strip())
    return params


def _images(tree: HTMLParser, data: dict) -> list[str]:
    raw_urls = _collect_raw_card_image_urls(tree, data)
    return _filter_and_rank_card_images(raw_urls)


def _seller(tree: HTMLParser, data: dict) -> tuple[str | None, str | None]:
    seller = data.get("seller") or {}
    seller_id = None
    seller_type = None
    if isinstance(seller, dict):
        seller_id = str(seller.get("id") or seller.get("userHash") or "") or None
        seller_type = str(seller.get("kind") or seller.get("type") or "").lower() or None
    link = tree.css_first("a[data-marker='seller-info/name'], a[href*='/user/']")
    if link is not None and not seller_id:
        href = link.attributes.get("href") or ""
        m = re.search(r"/user/([^/?]+)", href)
        if m:
            seller_id = m.group(1)
    return seller_id, seller_type


def extract_card_price(payload: str, *, url: str = "") -> tuple[int | None, str]:
    low = (payload or "").lower()
    if any(s in low for s in (
        'снято с '
        'публикации', "объявление закрыто", "страница не найдена", "404 not found")):
        return None, "closed"
    try:
        card = parse_card(payload, url=url)
        if card and card.price is not None and card.price > 0:
            return card.price, "ok"
    except Exception:
        pass
    return None, "unknown"

