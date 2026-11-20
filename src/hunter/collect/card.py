from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from hunter.collect.browser import BrowserSession
from hunter.collect.parsers.card import parse_card
from hunter.core.config import get_settings
from hunter.core.models import Card, FetchResult
from hunter.core.rawstore import write_raw


async def _fetch_card_mobile(
    avito_id: str, url: str, timeout_sec: float = 6.0, proxy: str | None = None,
) -> tuple[FetchResult, Card | None, Path] | None:
    """Fetches card payload from lightweight API."""
    mobile_url = f"https://www.avito.ru/api/1/card/items/{avito_id}"
    headers = {
        "accept": "application/json, text/plain, */*",
        "referer": "https://www.avito.ru/",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    t0 = time.monotonic()
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=True, proxy=proxy) as client:
            resp = await client.get(mobile_url, headers=headers)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                raw_text = resp.text
                if '"success"' in raw_text:
                    card = parse_card(raw_text, url=url)
                    if card:
                        raw_path = write_raw(
                            get_settings().raw_dir, avito_id, "card_mobile", raw_text
                        )
                        fetched = FetchResult(
                            url=mobile_url,
                            status=200,
                            html=raw_text,
                            final_url=url,
                            latency_ms=latency_ms,
                        )
                        return fetched, card, raw_path
    except Exception as exc:
        logger.debug("Mobile card fetch failed for {}: {}", avito_id, exc)
    return None


async def fetch_card(
    browser: BrowserSession, url: str, avito_id: str
) -> tuple[FetchResult, Card | None, Path]:
    settings = get_settings()
    mobile_enabled = getattr(getattr(settings, "collect", None), "mobile_card_api", True)

    if mobile_enabled and avito_id and avito_id.isdigit():
        res = await _fetch_card_mobile(avito_id, url)
        if res and res[1] and res[1].description:
            logger.debug("Card {} fetched via mobile JSON API ({}ms)", avito_id, res[0].latency_ms)
            return res

    fetched = await browser.fetch(url)
    raw_path = write_raw(settings.raw_dir, avito_id, "card", fetched.html)
    card = parse_card(fetched.html, url=fetched.final_url or url)
    return fetched, card, raw_path
