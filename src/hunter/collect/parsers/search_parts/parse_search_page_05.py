from __future__ import annotations

import json
import math
from typing import Any

from loguru import logger

from hunter.collect.parsers.search_parts.item_id_re_01 import SearchHits
from hunter.core.models import SearchHit


def _resolve_anchor_fallback(anchor_fallback_enabled: bool | None) -> bool:
    if anchor_fallback_enabled is not None:
        return anchor_fallback_enabled
    try:
        from hunter.core.config import get_settings
        settings = get_settings()
        return bool(getattr(settings, "parser", {}).get("anchor_fallback_enabled", False))
    except Exception:
        return False


def _extract_json_count(json_blob: Any) -> tuple[int | None, int | None]:
    if not isinstance(json_blob, dict):
        return None, None
    cnt = (
        json_blob.get("totalCount")
        or json_blob.get("count")
        or json_blob.get("total")
        or (json_blob.get("catalog", {}).get("totalCount") if isinstance(json_blob.get("catalog"), dict) else None)
        or (json_blob.get("data", {}).get("totalCount") if isinstance(json_blob.get("data"), dict) else None)
    )
    if isinstance(cnt, (int, float)) and cnt > 0:
        return int(cnt), math.ceil(int(cnt) / 50)
    return None, None


def _parse_pure_json_catalog(
    scrubbed: str,
    base_url: str,
    is_json_empty_fn: Any,
    walk_fn: Any,
    hit_fn: Any,
    is_known_path_fn: Any,
) -> tuple[SearchHits, str] | None:
    stripped = scrubbed.strip()
    if not ((stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]"))):
        return None
    try:
        json_blob = json.loads(stripped)
        if isinstance(json_blob, (dict, list)):
            if is_json_empty_fn(json_blob):
                hit_list = SearchHits([])
                hit_list.total_count = hit_list.total_pages = 0
                return hit_list, "empty_ok"

            json_hits: dict[str, SearchHit] = {}
            discarded = 0
            for raw, path, in_carousel in walk_fn(json_blob):
                hit = hit_fn(raw, base_url)
                if not hit:
                    continue
                if in_carousel:
                    discarded += 1
                    continue
                if is_known_path_fn(path) or not path:
                    if hit.avito_id not in json_hits:
                        json_hits[hit.avito_id] = hit
                else:
                    discarded += 1

            if json_hits:
                hit_list = SearchHits(json_hits.values())
                hit_list.accepted = hit_list.dom_hits = len(json_hits)
                hit_list.carousel_discarded, hit_list.xhr_extra = discarded, 0
                hit_list.total_count, hit_list.total_pages = _extract_json_count(json_blob)
                return hit_list, "json_items"
            elif discarded > 0 and not json_hits:
                hit_list = SearchHits([])
                hit_list.carousel_discarded = discarded
                return hit_list, "empty_ok"
    except Exception as exc:
        logger.debug("Failed parsing pure JSON in parse_search_page: {}", exc)
    return None


def _parse_mfe_state_fast_path(scrubbed: str, base_url: str) -> tuple[SearchHits, str] | None:
    if "data-mfe-state" not in scrubbed:
        return None
    try:
        from hunter.collect.parsers.mfe_state import extract_mfe_state_json, parse_mfe_state

        mfe_hits = parse_mfe_state(scrubbed, base_url=base_url)
        if mfe_hits:
            hit_list = SearchHits(mfe_hits)
            hit_list.accepted = hit_list.dom_hits = len(mfe_hits)
            hit_list.carousel_discarded = hit_list.xhr_extra = 0
            state_data = (
                (extract_mfe_state_json(scrubbed) or {})
                .get("loaderData", {})
                .get("data", {})
            )
            cnt = state_data.get("count") or state_data.get("totalCount")
            if isinstance(cnt, (int, float)) and cnt > 0:
                hit_list.total_count = int(cnt)
                hit_list.total_pages = math.ceil(int(cnt) / 50)
            return hit_list, "mfe_state"
    except Exception as exc:
        logger.debug("Failed fast-path mfe_state parse, falling back to DOM: {}", exc)
    return None


def _collect_dom_grid_hits(
    tree: Any,
    base_url: str,
    is_carousel_fn: Any,
    hit_from_node_fn: Any,
) -> tuple[dict[str, SearchHit], set[str]]:
    hits: dict[str, SearchHit] = {}
    grid_nodes = tree.css("[data-marker='catalog-serp'] [data-marker='item']")
    if not grid_nodes:
        grid_nodes = [
            n for n in tree.css("[data-marker='item']")
            if not is_carousel_fn(n)
        ]
    dom_grid_ids: set[str] = set()
    for node in grid_nodes:
        if is_carousel_fn(node):
            continue
        hit = hit_from_node_fn(node, base_url)
        if hit:
            hits[hit.avito_id] = hit
            dom_grid_ids.add(hit.avito_id)
    return hits, dom_grid_ids
