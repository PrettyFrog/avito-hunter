from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from loguru import logger

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    from selectolax.parser import HTMLParser

from hunter.collect.parsers.search_parts.hit_from_anchor_14 import (
    _hit_from_anchor,
    extract_avito_id as extract_avito_id,
)
from hunter.collect.parsers.search_parts.hit_from_json_12 import (
    _coerce_price,
    _extract_json_description,
    _extract_json_photos,
    _extract_json_published_at,
    _hit_from_node,
)
from hunter.collect.parsers.search_parts.item_id_re_01 import (
    INITIAL_RE as INITIAL_RE,
    ITEM_ID_RE as ITEM_ID_RE,
    PRICE_RE as PRICE_RE,
    _CUTOFF_RE,
    ParsedPrice,
    SearchHits,
    _extract_all_rub_tokens as _extract_all_rub_tokens,
    _extract_first_rub_token as _extract_first_rub_token,
    _is_carousel_or_recommendation,
    _parse_price_html,
    _to_int as _to_int,
)
from hunter.collect.parsers.search_parts.json_blobs_10 import (
    _extract_json_discounts,
    _extract_json_seller_and_city,
    _is_known_catalog_path,
    _json_blobs,
    _loads as _loads,
    _looks_like_item as _looks_like_item,
    _walk_items as _walk_items,
    _walk_items_with_meta,
)
from hunter.collect.parsers.search_parts.parse_price_02 import (
    _parse_price_numeric,
    _parse_price_str,
)
from hunter.collect.parsers.search_parts.parse_search_page_05 import (
    _collect_dom_grid_hits,
    _parse_mfe_state_fast_path,
    _parse_pure_json_catalog,
    _resolve_anchor_fallback,
)
from hunter.collect.parsers.search_parts.parse_search_page_07 import (
    _detect_empty_search_page,
    _extract_page_counts,
    _is_json_empty,
    _process_embedded_json_blobs,
    _process_xhr_blobs,
    empty_field_ratio as empty_field_ratio,
    price_missing_ratio as price_missing_ratio,
)
from hunter.core.models import SearchHit
from hunter.core.rawstore import scrub_text


def parse_price(node: Any) -> ParsedPrice:
    if node is None:
        return ParsedPrice(None, None, None, False, "missing")

    if isinstance(node, (int, float)):
        return _parse_price_numeric(node)

    if isinstance(node, str):
        return _parse_price_str(node)

    return _parse_price_html(node)


def parse_search(
    html: str,
    *,
    base_url: str = "https://www.avito.ru",
    extra_json: list[Any] | None = None,
    anchor_fallback_enabled: bool | None = None,
) -> list[SearchHit]:
    hits, _ = parse_search_page(
        html,
        base_url=base_url,
        extra_json=extra_json,
        anchor_fallback_enabled=anchor_fallback_enabled,
    )
    return hits


def _apply_anchor_fallback(
    tree: Any,
    base_url: str,
    hits: dict[str, SearchHit],
    anchor_fallback_enabled: bool,
) -> None:
    anchor_candidates = [
        n for n in tree.css("a[href*='_']")
        if not _is_carousel_or_recommendation(n)
    ]
    anchor_count = len(anchor_candidates)
    if not hits:
        if anchor_fallback_enabled:
            for node in anchor_candidates:
                hit = _hit_from_anchor(node, base_url)
                if hit and not _is_carousel_or_recommendation(node):
                    hit.extra["source"] = "anchor_fallback"
                    hits.setdefault(hit.avito_id, hit)
            if hits:
                logger.info("Found {} hits via anchor fallback", len(hits))
        elif anchor_count > 0:
            logger.info(
                "Anchor fallback disabled (found {} candidate anchor links; ignored)",
                anchor_count,
            )


def parse_search_page(
    html: str,
    *,
    base_url: str = "https://www.avito.ru",
    extra_json: list[Any] | None = None,
    anchor_fallback_enabled: bool | None = None,
) -> tuple[list[SearchHit], str]:
    anchor_fallback_enabled = _resolve_anchor_fallback(anchor_fallback_enabled)
    scrubbed = scrub_text(html)

    # Fast path 0: Pure JSON catalog response (e.g. from /web/1/js/items or API endpoints)
    json_result = _parse_pure_json_catalog(
        scrubbed, base_url, _is_json_empty, _walk_items_with_meta, _hit_from_json, _is_known_catalog_path
    )
    if json_result is not None:
        return json_result

    mfe_result = _parse_mfe_state_fast_path(scrubbed, base_url)
    if mfe_result is not None:
        return mfe_result

    cutoff_m = _CUTOFF_RE.search(scrubbed)
    dom_scrubbed = scrubbed[:cutoff_m.start()] if cutoff_m else scrubbed
    tree = HTMLParser(dom_scrubbed)
    hits, dom_grid_ids = _collect_dom_grid_hits(
        tree, base_url, _is_carousel_or_recommendation, _hit_from_node
    )

    embedded_blobs = list(_json_blobs(scrubbed))
    discarded_carousel_count = _process_embedded_json_blobs(
        embedded_blobs, base_url, dom_grid_ids, hits, _walk_items_with_meta, _hit_from_json, _is_known_catalog_path
    )
    xhr_discarded, xhr_extra, xhr_extra_blobs = _process_xhr_blobs(
        extra_json, base_url, hits, _walk_items_with_meta, _hit_from_json
    )
    discarded_carousel_count += xhr_discarded

    _apply_anchor_fallback(tree, base_url, hits, anchor_fallback_enabled)

    hit_list = SearchHits(hits.values())
    hit_list.carousel_discarded = discarded_carousel_count
    hit_list.dom_hits = len(dom_grid_ids)
    hit_list.xhr_extra = xhr_extra
    hit_list.accepted = len(hit_list)
    hit_list.xhr_extra_blobs = xhr_extra_blobs

    hit_list.total_count, hit_list.total_pages = _extract_page_counts(tree)
    if hit_list:
        return hit_list, "grid"

    verdict = _detect_empty_search_page(
        tree, scrubbed, extra_json, _json_blobs, cutoff_m, hits, HTMLParser
    )
    return hit_list, verdict


def _hit_from_json(node: dict, base_url: str) -> SearchHit | None:
    ident = str(node.get("id") or node.get("itemId") or node.get("avitoId") or "")
    if not ident.isdigit():
        return None
    title = node.get("title") or node.get("name")
    if isinstance(title, dict):
        title = title.get("text") or title.get("value") or title.get("title")
    url = node.get("urlPath") or node.get("url") or node.get("uri") or ""
    if url and not url.startswith("http"):
        url = urljoin(base_url, url)
    if not url:
        url = urljoin(base_url, f"/item/{ident}")
    raw_price = _coerce_price(node)
    parsed = parse_price(raw_price)

    final_old_price, final_discount_pct = _extract_json_discounts(node, parsed)
    seller, city = _extract_json_seller_and_city(node)

    photos = _extract_json_photos(node)
    desc_str, desc_truncated = _extract_json_description(node)
    published_at = _extract_json_published_at(node)

    hit = SearchHit(
        avito_id=ident,
        url=url,
        title=str(title) if title else None,
        price=parsed.price,
        old_price=final_old_price,
        discount_pct=final_discount_pct,
        price_suspect=parsed.price_suspect,
        price_state=parsed.price_state,
        city=str(city) if city else None,
        seller_type=seller,
        snippet=desc_str[:400] if desc_str else None,
        published_at=published_at,
    )
    if photos:
        hit.extra["photos"] = photos
    if desc_str:
        hit.extra["description_preview"] = desc_str
        hit.extra["desc_truncated"] = desc_truncated
    rating_obj = node.get("rating")
    if isinstance(rating_obj, dict):
        hit.extra["rating_score"] = rating_obj.get("score")
        hit.extra["rating_reviews"] = rating_obj.get("summary")
    if node.get("isReserved") or node.get("reserved"):
        hit.extra["is_reserved"] = True
    if any(k in node for k in ("delivery", "isDelivery", "hasDelivery")):
        hit.extra["delivery"] = bool(node.get("delivery") or node.get("isDelivery") or node.get("hasDelivery"))
    return hit

