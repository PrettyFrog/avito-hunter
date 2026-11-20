from __future__ import annotations

import re
from typing import Any

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    from selectolax.parser import HTMLParser

from hunter.collect.parsers.search import parse_search
from hunter.core.models import SearchHit


def check_block_markers(html: str, final_url: str, status: int) -> str | None:
    """Detect signs of captcha, firewall, block, or loss of auth in response.
    
    Returns marker string if blocked, None if clean.
    """
    html_lower = (html or "").lower()
    url_lower = (final_url or "").lower()

    if status == 439 or "firewallpow" in html_lower or (status != 200 and "challenge" in html_lower and "firewall" in html_lower):
        return "firewall_pow"

    if any(k in url_lower for k in ("/login", "auth.avito.ru", "passport.avito.ru")):
        return "login_redirect"
    if "captcha" in url_lower:
        return "captcha_url"
    if "firewall" in url_lower or "blocked" in url_lower:
        return "block_url"

    from hunter.collect.captcha_pause import looks_like_captcha
    if looks_like_captcha(html, final_url):
        if any(marker in url_lower for marker in ("/show/captcha", "captcha")):
            return "captcha_url"
        if "доступ ограничен" in html_lower or "доступ временно ограничен" in html_lower:
            return "access_denied_body"
        return "captcha_body"

    if "qrator" in html_lower:
        return "qrator_body"
    if "доступ ограничен" in html_lower or "доступ временно ограничен" in html_lower:
        return "access_denied_body"
    if "firewall" in html_lower and ("security" in html_lower or "cloudflare" in html_lower):
        return "firewall_body"
    if any(marker in html_lower for marker in (
        'data-marker="captcha"',
        'class="captcha',
        'id="captcha',
        'id="h-captcha',
        "smartcaptcha",
        "hcaptcha",
        "h-captcha",
        "geetest",
        "подтвердите, что вы не робот",
    )):
        return "captcha_body"

    if status in (401, 403):
        return f"status_{status}"
    if status != 200:
        return f"status_{status}"

    return None


def parse_shadow_search(html: str, base_url: str = "https://www.avito.ru") -> tuple[list[SearchHit], bool]:
    """Parse search hits using unmodified production parser and external date/geo adapter.
    
    Returns: (hits, has_grid)
    """
    stripped = (html or "").strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        hits = parse_search(html, base_url=base_url)
        return hits, len(hits) > 0

    tree = HTMLParser(html)
    grid_container = tree.css_first("[data-marker='catalog-serp']")
    item_nodes = tree.css("[data-marker='item']")
    has_grid = bool(grid_container or item_nodes)

    # Use unmodified production parse_search
    hits = parse_search(html, base_url=base_url)

    # External adapter for date and city enrichment without touching parsers/search.py
    date_geo_map: dict[str, tuple[str | None, str | None]] = {}
    for node in item_nodes:
        item_id = node.attributes.get("data-item-id")
        if not item_id:
            link = node.css_first("a[href*='_']")
            if link:
                m = re.search(r"_(\d{9,12})", link.attributes.get("href") or "")
                if m:
                    item_id = m.group(1)
        if item_id:
            d_node = node.css_first("[data-marker*='date']")
            g_node = node.css_first("[data-marker*='location'], [class*='geo']")
            date_geo_map[item_id] = (
                d_node.text(strip=True) if d_node else None,
                g_node.text(strip=True) if g_node else None,
            )

    CITY_FROM_TITLE_RE = re.compile(r"\sв\s([А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+)?)$")
    for hit in hits:
        if hit.avito_id in date_geo_map:
            d_val, g_val = date_geo_map[hit.avito_id]
            if d_val:
                hit.extra["date"] = d_val
            if not hit.city and g_val:
                hit.city = g_val
        if not hit.city and hit.title:
            m = CITY_FROM_TITLE_RE.search(hit.title)
            if m:
                # Prepositional case city, used strictly for display
                hit.city = m.group(1)

    return hits, has_grid


def calculate_field_coverage(hits: list[SearchHit]) -> dict[str, float]:
    """Calculate fill ratio for critical and secondary fields across search hits."""
    if not hits:
        return {}
    n = float(len(hits))
    return {
        "title": sum(1 for h in hits if h.title) / n,
        "price": sum(1 for h in hits if h.price is not None) / n,
        "old_price": sum(1 for h in hits if h.old_price is not None) / n,
        "discount_pct": sum(1 for h in hits if h.discount_pct is not None) / n,
        "url": sum(1 for h in hits if h.url) / n,
        "seller_type": sum(1 for h in hits if h.seller_type) / n,
        "price_state": sum(1 for h in hits if h.price_state and h.price_state != "missing") / n,
        "date": sum(1 for h in hits if h.extra.get("date")) / n,
    }


def check_field_coverage_drop(
    coverage: dict[str, float],
    baseline: dict[str, float],
    threshold_factor: float = 0.90,
    min_hits: int = 10,
    total_hits: int = 0,
) -> tuple[bool, str | None]:
    """Check if coverage of any critical field dropped below 90% of baseline.
    
    Critical fields: title, price, url, seller_type (if baseline >= 0.10).
    Returns: (is_dropped, reason)
    """
    if total_hits < min_hits or not coverage:
        return False, None

    critical_fields = ["title", "price", "url"]
    if baseline.get("seller_type", 0.0) >= 0.10:
        critical_fields.append("seller_type")

    for field in critical_fields:
        b_val = baseline.get(field, 0.0)
        c_val = coverage.get(field, 0.0)
        if b_val > 0.0 and c_val < (b_val * threshold_factor):
            return True, f"field coverage drop for '{field}': {c_val:.2f} < {b_val * threshold_factor:.2f} (baseline {b_val:.2f})"

    return False, None


def compare_hits(normal_hits: list[SearchHit], shadow_hits: list[SearchHit]) -> dict[str, Any]:
    """Compare shadow hits against normal poll hits.
    
    Returns metrics dict. When baseline or prices are missing, returns None instead of 100%.
    """
    normal_ids = [h.avito_id for h in normal_hits]
    shadow_ids = [h.avito_id for h in shadow_hits]

    n_set = set(normal_ids)
    s_set = set(shadow_ids)
    overlap = len(n_set & s_set)
    n_extra = len(s_set - n_set)

    # id_overlap_pct: None if normal hits is empty (no denominator)
    if not n_set:
        id_overlap_pct = None
    else:
        id_overlap_pct = round(overlap / len(n_set) * 100.0, 2)

    # same_order_top20: None if normal hits has no items
    top20_norm = normal_ids[:20]
    top20_shadow = shadow_ids[:20]
    if not top20_norm:
        same_order_top20 = None
    else:
        same_order_top20 = 1 if (top20_norm == top20_shadow) else 0

    # price_equal_pct: None if no common priced items
    norm_prices = {h.avito_id: h.price for h in normal_hits if h.price is not None}
    shadow_prices = {h.avito_id: h.price for h in shadow_hits if h.price is not None}
    common_price_ids = set(norm_prices.keys()) & set(shadow_prices.keys())
    if not common_price_ids:
        price_equal_pct = None
    else:
        matches = sum(1 for cid in common_price_ids if norm_prices[cid] == shadow_prices[cid])
        price_equal_pct = round(matches / len(common_price_ids) * 100.0, 2)

    return {
        "n_items": len(shadow_hits),
        "n_with_price": sum(1 for h in shadow_hits if h.price is not None),
        "n_extra": n_extra,
        "id_overlap_pct": id_overlap_pct,
        "same_order_top20": same_order_top20,
        "price_equal_pct": price_equal_pct,
    }
