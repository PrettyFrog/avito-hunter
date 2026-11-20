from __future__ import annotations

import math
import re
from typing import Any

from loguru import logger

from hunter.core.models import SearchHit


def _enrich_hit(target: SearchHit, source: SearchHit, include_city: bool = False) -> None:
    if not target.title and source.title:
        target.title = source.title
    if not target.seller_type and source.seller_type:
        target.seller_type = source.seller_type
    if not target.snippet and source.snippet:
        target.snippet = source.snippet
    if target.price is None and source.price is not None:
        target.price = source.price
        target.price_state = source.price_state
    if include_city and not target.city and source.city:
        target.city = source.city


def _process_embedded_json_blobs(
    embedded_blobs: list[Any],
    base_url: str,
    dom_grid_ids: set[str],
    hits: dict[str, SearchHit],
    walk_fn: Any,
    hit_fn: Any,
    is_known_path_fn: Any,
) -> int:
    discarded = 0
    for blob in embedded_blobs:
        for raw, path, in_carousel in walk_fn(blob):
            hit = hit_fn(raw, base_url)
            if not hit:
                continue
            in_dom = hit.avito_id in dom_grid_ids
            in_catalog = is_known_path_fn(path) and not in_carousel

            if in_carousel or (not in_dom and not in_catalog):
                discarded += 1
                continue

            if hit.avito_id not in hits:
                hits[hit.avito_id] = hit
            else:
                _enrich_hit(hits[hit.avito_id], hit)
    return discarded


def _process_xhr_blobs(
    extra_json: list[Any] | None,
    base_url: str,
    hits: dict[str, SearchHit],
    walk_fn: Any,
    hit_fn: Any,
) -> tuple[int, int, list[Any]]:
    discarded = 0
    xhr_extra = 0
    xhr_extra_blobs = []
    for blob in (extra_json or []):
        blob_had_extra = False
        resp_url = blob.get("_response_url") if isinstance(blob, dict) else None
        for raw, _, _ in walk_fn(blob):
            hit = hit_fn(raw, base_url)
            if not hit:
                continue
            if hit.avito_id in hits:
                if resp_url:
                    logger.info("XHR URL enriched search item {}: {}", hit.avito_id, resp_url)
                _enrich_hit(hits[hit.avito_id], hit, include_city=True)
            else:
                discarded += 1
                xhr_extra += 1
                blob_had_extra = True
        if blob_had_extra:
            xhr_extra_blobs.append(blob)
    return discarded, xhr_extra, xhr_extra_blobs


def _extract_page_counts(tree: Any) -> tuple[int | None, int | None]:
    cnt_node = tree.css_first(
        "span[data-marker='page-title/count'], "
        "[data-marker='page-title/count']")
    total_count = None
    if cnt_node:
        raw_cnt = re.sub(r"[^\d]", "", cnt_node.text(strip=True))
        if raw_cnt:
            total_count = int(raw_cnt)

    page_links = [a.attributes.get("href") for a in tree.css("a[href*='p=']")]
    page_nums = [
        int(m.group(1))
        for l in page_links
        if l and (m := re.search(r"[?&]p=(\d+)", l))
    ]
    if page_nums:
        total_pages = max(page_nums)
    elif total_count is not None:
        total_pages = math.ceil(total_count / 50) if total_count > 0 else 1
    else:
        total_pages = None
    return total_count, total_pages


def _detect_empty_search_page(
    tree: Any,
    scrubbed: str,
    extra_json: list[Any] | None,
    json_blobs_fn: Any,
    cutoff_m: Any,
    hits: dict[str, SearchHit],
    parser_cls: Any,
) -> str:
    serp = tree.css_first("[data-marker='catalog-serp']")
    has_empty_marker = (
        tree.css_first(
            "[data-marker='empty-page'], [class*='empty-page'], "
            "[class*='no-results']") is not None
    )
    visible_tree = parser_cls(scrubbed)
    for hidden_node in visible_tree.css("script, style, noscript, template"):
        hidden_node.decompose()
    visible_body = visible_tree.body or visible_tree.root
    visible_text = visible_body.text(separator=" ") if visible_body is not None else ""
    text_lower = visible_text.lower()
    has_empty_text = any(
        k in text_lower
        for k in (
            "ничего не найдено",
            "по вашему запросу ничего не нашлось",
            "попробуйте изменить запрос",
            "попробуйте убрать фильтры",
            "похоже, таких объявлений пока нет",
        )
    )
    all_blobs: list[Any] = list(extra_json or [])
    all_blobs.extend(json_blobs_fn(scrubbed))
    json_empty = any(_is_json_empty(b, depth=0, max_depth=3) for b in all_blobs)

    is_explicit_empty = has_empty_marker or has_empty_text or json_empty or (cutoff_m is not None and len(hits) == 0)
    has_catalog_serp = serp is not None

    if is_explicit_empty and (has_catalog_serp or has_empty_marker or cutoff_m is not None):
        return "empty_ok"
    return "unknown_empty"


def empty_field_ratio(hits: list[SearchHit]) -> float:
    if not hits:
        return 1.0
    empty = 0
    total = 0
    for hit in hits:
        for value in (hit.title, hit.url):
            total += 1
            if value in (None, "", 0):
                empty += 1
    return empty / total if total else 1.0


def price_missing_ratio(hits: list[SearchHit]) -> float:
    if not hits:
        return 0.0
    missing = sum(1 for hit in hits if getattr(hit, "price_state", "missing") == "missing")
    return missing / len(hits)


def _is_json_empty(obj: Any, depth: int = 0, max_depth: int = 3) -> bool:
    if not isinstance(obj, dict) or not obj:
        return False

    for items_key in ("items", "cards", "elements", "entries", "results", "catalog"):
        v = obj.get(items_key)
        if isinstance(v, list) and len(v) > 0:
            return False
        if isinstance(v, dict):
            sub = v.get("items") or v.get("elements") or v.get("list")
            if isinstance(sub, list) and len(sub) > 0:
                return False

    if "items" in obj and isinstance(obj["items"], list) and len(obj["items"]) == 0:
        for k in ("total", "count", "totalCount"):
            if k in obj:
                val = obj[k]
                if val == 0 or (isinstance(val, str) and val.strip() == "0"):
                    return True

    for scope_key in ("catalog", "serp", "search", "items"):
        sub = obj.get(scope_key)
        if isinstance(sub, dict):
            for k in ("total", "count", "totalCount"):
                if k in sub:
                    val = sub[k]
                    if val == 0 or (isinstance(val, str) and val.strip() == "0"):
                        sub_items = sub.get("items") or sub.get("elements")
                        if sub_items is None or (isinstance(sub_items, list) and len(sub_items) == 0):
                            return True
            for inner_key in ("result", "search", "items", "catalog"):
                sub2 = sub.get(inner_key)
                if isinstance(sub2, dict):
                    for k in ("total", "count", "totalCount"):
                        if k in sub2:
                            val = sub2[k]
                            if val == 0 or (isinstance(val, str) and val.strip() == "0"):
                                sub2_items = sub2.get("items") or sub2.get("elements")
                                if sub2_items is None or (isinstance(sub2_items, list) and len(sub2_items) == 0):
                                    return True

    if depth < max_depth:
        for root_key in ("data", "response"):
            data_sub = obj.get(root_key)
            if isinstance(data_sub, dict):
                if _is_json_empty(data_sub, depth=depth + 1, max_depth=max_depth):
                    return True

    return False
