from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hunter.collect.parsers.mfe_state import _best_photo_url
from hunter.collect.parsers.search_parts.item_id_re_01 import _to_int
from hunter.core.models import SearchHit


def _extract_json_photos(node: dict[str, Any]) -> list[str]:
    photos: list[str] = []

    raw_images = node.get("images")
    if isinstance(raw_images, list):
        for img_entry in raw_images:
            if isinstance(img_entry, dict):
                best = _best_photo_url(img_entry)
                if best:
                    photos.append(best)
            elif isinstance(img_entry, str) and img_entry.startswith("http"):
                photos.append(img_entry)
    elif isinstance(raw_images, dict):
        best = _best_photo_url(raw_images)
        if best:
            photos.append(best)

    raw_gallery = node.get("gallery")
    if isinstance(raw_gallery, list) and not photos:
        for gal_entry in raw_gallery:
            if isinstance(gal_entry, dict):
                best = _best_photo_url(gal_entry)
                if best:
                    photos.append(best)
            elif isinstance(gal_entry, str) and gal_entry.startswith("http"):
                photos.append(gal_entry)

    if not photos:
        for img_k in ("image_url", "imageUrl", "image", "photo"):
            val = node.get(img_k)
            if isinstance(val, str) and val.startswith("http"):
                photos.append(val)
                break
    return photos


def _extract_json_description(node: dict[str, Any]) -> tuple[str, bool]:
    raw_desc = node.get("description") or node.get("snippet") or ""
    if not raw_desc:
        iva = node.get("iva")
        if isinstance(iva, dict):
            step = iva.get("DescriptionStep")
            if isinstance(step, list) and step and isinstance(step[0], dict):
                raw_desc = step[0].get("payload", {}).get("description", "") or ""
    desc_str = str(raw_desc).strip()
    desc_truncated = False
    if desc_str:
        if desc_str.endswith("...") or desc_str.endswith("…") or node.get("isTruncated"):
            desc_truncated = True
    return desc_str, desc_truncated


def _extract_json_published_at(node: dict[str, Any]) -> str | None:
    sort_ts = node.get("sortTimeStamp") or node.get("allowTimeStamp") or node.get("time")
    if sort_ts and isinstance(sort_ts, (int, float)):
        try:
            sec = (sort_ts / 1000.0) if sort_ts > 1_000_000_000_000 else float(sort_ts)
            pub_dt = datetime.fromtimestamp(sec, tz=UTC)
            return pub_dt.isoformat()
        except Exception:
            return None
    return None


def _coerce_price(node: dict[str, Any]) -> int | None:
    # Prefer buyer price fields to avoid the pre-discount price trap.
    iva = node.get("iva") or {}
    if isinstance(iva, dict):
        p_step = iva.get("PriceStep") or iva.get("priceStep")
        if isinstance(p_step, list) and p_step:
            payload = p_step[0].get("payload", {}) if isinstance(p_step[0], dict) else {}
            if isinstance(payload, dict):
                step_val = _to_int(payload.get("price") or payload.get("value"))
                if step_val is not None and step_val > 0:
                    return step_val
        elif isinstance(p_step, dict):
            step_val = _to_int(p_step.get("price") or p_step.get("value"))
            if step_val is not None and step_val > 0:
                return step_val

    if "discountPrice" in node:
        disc_val = _to_int(node.get("discountPrice"))
        if disc_val is not None and disc_val > 0:
            return disc_val

    if "formattedPrice" in node and isinstance(node["formattedPrice"], dict):
        fp_val = _to_int(node["formattedPrice"].get("value"))
        if fp_val is not None and fp_val > 0:
            return fp_val

    detailed = node.get("priceDetailed") or node.get("price")
    if isinstance(detailed, dict):
        for key in ("value", "current", "normalized", "metric"):
            if key in detailed and isinstance(detailed[key], dict):
                inner = detailed[key]
                if "value" in inner:
                    val = _to_int(inner["value"])
                    if val is not None and val > 0:
                        return val
            elif key in detailed:
                val = _to_int(detailed[key])
                if val is not None and val > 0:
                    return val
        for str_key in ("fullString", "string", "text", "raw"):
            if str_key in detailed:
                val = _to_int(detailed[str_key])
                if val is not None and val > 0:
                    return val
        val = _to_int(detailed.get("value"))
        if val is not None and val > 0:
            return val
        if "price" in node:
            val = _to_int(node.get("price"))
            if val is not None and val > 0:
                return val
    return _to_int(detailed)


def _hit_from_node(node: Any, base_url: str, anchor_fn: Any = None) -> SearchHit | None:
    link = (
        node.css_first("a[data-marker='item-title']")
        or node.css_first("h3 a")
        or node.css_first("[itemprop='name'] a")
        or node.css_first("a[itemprop='url']")
        or node.css_first("a[href*='_']")
        or node.css_first("a[href]")
    )
    if link is None:
        return None
    if anchor_fn is None:
        from hunter.collect.parsers.search import _hit_from_anchor as anchor_fn
    return anchor_fn(link, base_url, root=node)

