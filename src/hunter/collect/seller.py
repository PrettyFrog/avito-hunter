from __future__ import annotations

from datetime import timedelta

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    from selectolax.parser import HTMLParser

from hunter.collect.browser import BrowserSession
from hunter.collect.parsers.search import extract_avito_id
from hunter.core.config import get_settings
from hunter.core.db import Database
from hunter.core.models import now_utc, utcnow


async def fetch_seller_if_needed(
    db: Database,
    browser: BrowserSession,
    seller_id: str | None,
    profile_url: str | None,
) -> None:
    """Round 2 only. Cached for seller.cache_days. Never stores phone/name."""
    if not seller_id or not profile_url:
        return
    settings = get_settings()
    days = int((settings.seller or {}).get("cache_days") or 7)
    existing = await db.get_seller(seller_id)
    if existing and existing.get("fetched_at"):
        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        if str(existing["fetched_at"]) > cutoff:
            return
    fetched = await browser.fetch(profile_url)
    if fetched.captcha or fetched.blocked:
        return
    tree = HTMLParser(fetched.html)
    rating = None
    reviews = None
    registered = None
    rating_node = tree.css_first("[data-marker*='rating'], [itemprop='ratingValue']")
    if rating_node is not None:
        raw = rating_node.attributes.get("content") or rating_node.text()
        try:
            rating = float(str(raw).replace(",", ".").split()[0])
        except (TypeError, ValueError):
            rating = None
    await db.upsert_seller(
        {
            "seller_id": seller_id,
            "kind": None,
            "rating": rating,
            "reviews": reviews,
            "registered": registered,
            "listings_count": None,
            "fetched_at": utcnow(),
        }
    )


def seller_url_from_card_html(html: str) -> str | None:
    tree = HTMLParser(html)
    link = tree.css_first("a[href*='/user/']")
    if link is None:
        return None
    href = link.attributes.get("href") or ""
    if href.startswith("/"):
        return "https://www.avito.ru" + href
    return href or None


def listing_id_from_url(url: str) -> str | None:
    return extract_avito_id(url)
