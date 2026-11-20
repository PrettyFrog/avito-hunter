from __future__ import annotations

import gzip
import hashlib
import time
from pathlib import Path
from typing import Any

from hunter.collect.browser import BrowserSession
from hunter.collect.parsers.search import parse_search
from hunter.collect.search_parts.fetch_search_light_01 import (
    build_light_fetch_result,
    execute_light_variant,
)
from hunter.core.config import get_settings
from hunter.core.executor import run_cpu_bound
from hunter.core.models import FetchResult, SearchHit, now_utc
from hunter.core.rawstore import scrub_text, write_raw

__all__ = [
    "FetchResult",
    "build_search_page_url",
    "extract_search_page_num",
    "extract_search_page_url_num",
    "fetch_search",
    "fetch_search_light",
]


def _save_search_raw_sync(
    raw_dir: Path,
    data_dir: Path,
    sid: str,
    html: str,
    keep: int,
) -> None:
    if not html:
        return
    write_raw(raw_dir, sid, "search", html)
    if keep > 0:
        samples_dir = data_dir / "raw_samples" / sid
        samples_dir.mkdir(parents=True, exist_ok=True)
        ts = now_utc().strftime("%Y%m%d_%H%M%S")
        sample_path = samples_dir / f"{ts}_search.html.gz"
        sample_path.write_bytes(gzip.compress(scrub_text(html).encode("utf-8")))
        existing = sorted(samples_dir.glob("*_search.html.gz"))
        if len(existing) > keep:
            for old in existing[:-keep]:
                old.unlink(missing_ok=True)


def _save_and_parse_render_sync(
    raw_dir: Path,
    data_dir: Path,
    sid: str,
    html: str,
    keep: int,
    base_url: str,
    extra_json: list[Any] | None,
) -> list[SearchHit]:
    _save_search_raw_sync(raw_dir, data_dir, sid, html, keep)
    return parse_search(
        html,
        base_url=base_url,
        extra_json=extra_json,
    )


def _save_and_parse_light_sync(
    raw_dir: Path,
    data_dir: Path,
    sid: str,
    html: str,
    keep: int,
    base_url: str,
) -> tuple[list[SearchHit], bool]:
    from hunter.collect.lightfetch import parse_shadow_search

    _save_search_raw_sync(raw_dir, data_dir, sid, html, keep)
    return parse_shadow_search(html, base_url=base_url)


async def fetch_search(
    browser: BrowserSession,
    url: str,
    mode: str = "render",
    light_variant: str = "light_nav",
    profile_id: str | None = None,
) -> tuple[FetchResult, list[SearchHit]]:
    if mode == "light":
        res, hits, _, _ = await fetch_search_light(
            browser,
            url,
            variant=light_variant,
            profile_id=profile_id,
        )
        return res, hits

    fetched = await browser.fetch(url)
    settings = get_settings()
    sid = _search_id(url)
    canary_conf = settings.canary or {}
    keep = int(canary_conf.get("raw_samples_keep", 5))

    hits = await run_cpu_bound(
        _save_and_parse_render_sync,
        settings.raw_dir,
        settings.data_dir,
        sid,
        fetched.html,
        keep,
        _origin(url),
        fetched.json_blobs,
    )
    return fetched, hits


async def fetch_search_light(
    browser: BrowserSession,
    url: str,
    variant: str = "light_nav",
    profile_id: str | None = None,
) -> tuple[FetchResult, list[SearchHit], bool, str | None]:
    """Execute lightweight search retrieval without full page rendering.
    
    Returns: (FetchResult, hits, has_grid, error)
    """
    settings = get_settings()
    start_mono = time.monotonic()
    status, final_url, html, err = await execute_light_variant(
        browser=browser,
        url=url,
        variant=variant,
        profile_id=profile_id,
    )
    lat_ms = int((time.monotonic() - start_mono) * 1000)
    sid = _search_id(url)
    canary_conf = settings.canary or {}
    keep = int(canary_conf.get("raw_samples_keep", 5))

    # Offload raw HTML scrubbing, gzip compression, disk write, and selectolax parsing
    # to the CPU worker thread pool.
    hits, has_grid = await run_cpu_bound(
        _save_and_parse_light_sync,
        settings.raw_dir,
        settings.data_dir,
        sid,
        html,
        keep,
        _origin(url),
    )

    res = build_light_fetch_result(
        url=url,
        status=status,
        html=html,
        final_url=final_url,
        lat_ms=lat_ms,
        err=err,
        profile_id=profile_id,
    )
    return res, hits, has_grid, err


def _origin(url: str) -> str:
    if "://" in url:
        return "/".join(url.split("/")[:3])
    return "https://www.avito.ru"


def _search_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def build_search_page_url(url: str, page: int) -> str:
    """Build Avito search URL with page query parameter p=..."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page > 1:
        params["p"] = str(page)
    else:
        params.pop("p", None)
    new_query = urlencode(sorted(params.items()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def extract_search_page_url_num(url: str) -> int:
    return extract_search_page_num(url)


def extract_search_page_num(url: str) -> int:
    """Extract page number p from Avito search URL, default 1."""
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    try:
        return int(params.get("p", 1))
    except (ValueError, TypeError):
        return 1
