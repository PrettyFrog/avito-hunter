from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from hunter.collect.parsers.search_parts.item_id_re_01 import (
    ITEM_ID_RE,
    _is_carousel_or_recommendation,
)
from hunter.core.models import SearchHit


def _extract_anchor_title_and_id(
    node: Any, base_url: str, root: Any | None
) -> tuple[str, str, str | None] | None:
    if _is_carousel_or_recommendation(node):
        return None

    href = node.attributes.get("href") or ""
    match = ITEM_ID_RE.search(href)
    if not match:
        return None
    ident = match.group(1)
    url = href if href.startswith("http") else urljoin(base_url, href)

    title = None
    if root is not None:
        title_el = (
            root.css_first("a[data-marker='item-title']")
            or root.css_first("[itemprop='name']")
            or root.css_first("h3")
        )
        if title_el is not None:
            title = title_el.attributes.get("title") or title_el.text(strip=True)
    if not title or title.startswith("Ещё") or title.startswith("Еще") or "фото" in title.lower():
        title = node.attributes.get("title") or node.text(strip=True)
    if title and (title.startswith("Ещё") or title.startswith("Еще") or "фото" in title.lower()):
        title = None
    if not title and "/" in url:
        slug = url.split("?")[0].rstrip("/").split("/")[-1]
        if "_" in slug:
            clean_slug = slug.rsplit("_", 1)[0].replace("_", " ")
            if len(clean_slug) > 3:
                title = clean_slug
    return ident, url, title


def _resolve_anchor_scope(node: Any, root: Any | None) -> tuple[Any | None, bool]:
    scope = root
    if scope is None and node.parent is not None:
        p = node.parent
        for _ in range(4):
            if p is not None and (
                p.attributes.get("data-marker") == "item"
                or p.css_first("[data-marker='item-price'], [itemprop='price'], [class*='price']")
            ):
                if _is_carousel_or_recommendation(p):
                    return None, False
                scope = p
                break
            if p and p.parent:
                p = p.parent
            else:
                break
        if scope is None:
            scope = node.parent

    if _is_carousel_or_recommendation(scope):
        return None, False
    return scope, True


def _extract_scope_details(scope: Any, title: str | None) -> tuple[str | None, str | None]:
    if scope is not None and (not title or title.endswith("...") or len(title) < 8):
        img = scope.css_first("img[alt]")
        if img and img.attributes.get("alt"):
            alt = img.attributes.get("alt").strip()
            if len(alt) > len(title or ""):
                title = alt

    seller = None
    if scope is not None:
        seller_node = scope.css_first("[data-marker='item-line']")
        if seller_node is not None:
            text = seller_node.text(strip=True).lower()
            if any(k in text for k in ("магазин", "компани", "дилер", "shop", "dealer")):
                seller = "company"
            elif "частн" in text:
                seller = "private"
    return title, seller


def _extract_anchor_extra(scope: Any) -> tuple[dict[str, Any], str | None, str | None]:
    if scope is None:
        return {}, None, None
    extra: dict[str, Any] = {}
    photos: list[str] = []
    for img in scope.css("img"):
        src = img.attributes.get("src", "")
        if src.startswith("http") and "avito.st" in src and src not in photos:
            photos.append(src)
    for el in scope.css("[data-marker*='slider-image/image-']"):
        u = el.attributes.get("data-marker", "").replace("slider-image/image-", "")
        if u.startswith("http") and u not in photos:
            photos.append(u)
    if photos:
        extra["photos"] = photos
    desc_el = scope.css_first("p[style*='--module-max-lines-size'], [data-marker='item-description']")
    desc = desc_el.text(strip=True) if desc_el else None
    if desc:
        extra["description_preview"] = desc
    score_el = scope.css_first("[data-marker='seller-rating/score'], [data-marker='seller-info/score']")
    if score_el and score_el.text(strip=True):
        extra["rating_score"] = score_el.text(strip=True)
    summary_el = scope.css_first("[data-marker='seller-info/summary']")
    if summary_el and summary_el.text(strip=True):
        extra["rating_reviews"] = summary_el.text(strip=True)
    txt = (scope.text(strip=True) or "").lower()
    if scope.css_first("[data-marker*='delivery'], [class*='delivery']") or "доставк" in txt:
        extra["delivery"] = True
    if "забронирован" in txt:
        extra["is_reserved"] = True
    loc_el = scope.css_first("[data-marker='item-location'], [data-marker='item-address']")
    city = loc_el.text(strip=True) if loc_el and loc_el.text(strip=True) else None
    return extra, desc, city


def _hit_from_anchor(
    node: Any, base_url: str, root: Any | None = None, parse_price_fn: Any = None
) -> SearchHit | None:
    id_info = _extract_anchor_title_and_id(node, base_url, root)
    if id_info is None:
        return None
    ident, url, title = id_info

    scope, is_valid = _resolve_anchor_scope(node, root)
    if not is_valid:
        return None

    title, seller = _extract_scope_details(scope, title)

    price = None
    old_price = None
    discount_pct = None
    price_suspect = False
    price_state = "missing"
    if scope is not None:
        if parse_price_fn is None:
            from hunter.collect.parsers.search import parse_price as parse_price_fn
        parsed_price = parse_price_fn(scope)
        price = parsed_price.price
        old_price = parsed_price.old_price
        discount_pct = parsed_price.discount_pct
        price_suspect = parsed_price.price_suspect
        price_state = parsed_price.price_state

    extra, desc, city = _extract_anchor_extra(scope)
    return SearchHit(
        avito_id=ident,
        url=url,
        title=title or None,
        price=price,
        old_price=old_price,
        discount_pct=discount_pct,
        price_suspect=price_suspect,
        price_state=price_state,
        city=city,
        seller_type=seller,
        snippet=desc,
        extra=extra,
    )


def extract_avito_id(url: str) -> str | None:
    match = ITEM_ID_RE.search(url)
    return match.group(1) if match else None
