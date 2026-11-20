from __future__ import annotations

import json
import re
from typing import Any

from hunter.collect.parsers.search_parts.item_id_re_01 import ParsedPrice, _to_int

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    from selectolax.parser import HTMLParser


def _looks_like_item(node: dict) -> bool:
    ident = node.get("id") or node.get("itemId") or node.get("avitoId")
    title = node.get("title") or node.get("name")
    url = node.get("urlPath") or node.get("url") or node.get("uri")
    has_price = any(k in node for k in ("priceDetailed", "price", "sortTimeStamp"))
    if ident is None or not title:
        return False
    sid = str(ident)
    return sid.isdigit() and len(sid) >= 8 and (url or has_price)


def _is_known_catalog_path(path: tuple[str, ...]) -> bool:
    path_str = "/".join(path)
    if any(k in path_str for k in ("catalog", "serp", "search")):
        return True
    if any(k in path_str for k in ("items", "elements", "cards", "entries")):
        return True
    return False


def _walk_items_with_meta(obj: Any) -> list[tuple[dict, tuple[str, ...], bool]]:
    found: list[tuple[dict, tuple[str, ...], bool]] = []
    carousel_markers = (
        "cvr",
        "carousel",
        "recommendation",
        "recommendations",
        "popular",
        "similar",
        "widgets",
        "widget",
        "serp-banner",
        "banner",
        "популярн",
        "рекоменд",
    )

    def is_carousel_dict(d: dict) -> bool:
        for attr in ("type", "viewType", "component", "blockType", "name", "title"):
            val = str(d.get(attr) or "").lower()
            if any(m in val for m in carousel_markers):
                return True
        return False

    def rec(node: Any, path: tuple[str, ...], in_carousel: bool) -> None:
        if isinstance(node, dict):
            node_is_carousel = in_carousel or is_carousel_dict(node)
            if _looks_like_item(node):
                found.append((node, path, node_is_carousel))
            for k, value in node.items():
                k_lower = str(k).lower()
                child_in_carousel = node_is_carousel or any(m in k_lower for m in carousel_markers)
                rec(value, path + (k_lower,), child_in_carousel)
        elif isinstance(node, list):
            for value in node:
                rec(value, path, in_carousel)

    rec(obj, (), False)
    return found


def _walk_items(obj: Any) -> list[dict]:
    return [item for item, _, _ in _walk_items_with_meta(obj)]


def _loads(text: str) -> Any | None:
    text = text.strip().rstrip(";")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _json_blobs(html: str) -> list[Any]:
    blobs: list[Any] = []
    tree = HTMLParser(html)
    for script in tree.css("script"):
        text = script.text(strip=True) or ""
        if script.attributes.get("id") == "__NEXT_DATA__" or "application/json" in (
            script.attributes.get("type") or ""
        ):
            parsed = _loads(text)
            if parsed is not None:
                blobs.append(parsed)
                continue
        for match in re.finditer(
            r"(?:window\.__initialData__|window\.__state__)\s*=\s*(\{.*?\})\s*;",
            text,
            re.S,
        ):
            parsed = _loads(match.group(1))
            if parsed is not None:
                blobs.append(parsed)
        stripped = text.strip()
        if stripped.startswith("{") and '"id"' in stripped and len(stripped) < 5_000_000:
            parsed = _loads(stripped)
            if parsed is not None:
                blobs.append(parsed)
    return blobs


def _extract_json_discounts(node: dict, parsed: ParsedPrice) -> tuple[int | None, float | None]:
    old_p = None
    disc_pct = None
    if "priceWithoutDiscount" in node:
        old_p = _to_int(node.get("priceWithoutDiscount"))
    if not old_p and isinstance(node.get("priceDetailed"), dict):
        pd_val = _to_int(node["priceDetailed"].get("value"))
        if pd_val and parsed.price and pd_val > parsed.price:
            old_p = pd_val

    iva = node.get("iva") or {}
    if isinstance(iva, dict):
        p_step = iva.get("PriceStep") or iva.get("priceStep")
        if isinstance(p_step, list) and p_step and isinstance(p_step[0], dict):
            pay = p_step[0].get("payload", {})
            if isinstance(pay, dict):
                old_p = old_p or _to_int(pay.get("oldPrice") or pay.get("priceWithoutDiscount"))
                raw_pct = pay.get("discountPercent")
                if raw_pct is not None:
                    try:
                        disc_pct = float(raw_pct)
                    except (ValueError, TypeError):
                        pass

    if "discountPercent" in node and disc_pct is None:
        try:
            disc_pct = float(node.get("discountPercent"))
        except (ValueError, TypeError):
            pass

    final_old_price = parsed.old_price or old_p
    final_discount_pct = parsed.discount_pct or disc_pct
    if (
        final_old_price
        and parsed.price
        and final_old_price > parsed.price
        and not final_discount_pct
    ):
        final_discount_pct = round((final_old_price - parsed.price) / final_old_price * 100, 1)
    return final_old_price, final_discount_pct


def _extract_json_seller_and_city(node: dict) -> tuple[str | None, str | None]:
    seller = None
    seller_obj = node.get("seller") or node.get("iva") or {}
    if isinstance(seller_obj, dict):
        seller = str(
            seller_obj.get("kind")
            or seller_obj.get("type")
            or seller_obj.get("sellerType")
            or ""
        ).lower() or None
    from hunter.normalize.city import extract_item_city
    city = extract_item_city(node)
    return seller, city

