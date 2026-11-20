"""Resolver and mapper from canonical search URLs to lightweight Avito /web/1/js/items API."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import urllib.parse
from typing import Any

from hunter.collect.parsers.mfe_state import extract_mfe_state_json
from hunter.transport.classification import ResponseOutcome, classify_response
from loguru import logger

_SEED_ITEMS_URLS: dict[str, str] = {}


def build_items_url_from_html(canonical_url: str, html: str) -> str | None:
    """Extracts searchCore parameters from HTML and constructs /web/1/js/items endpoint URL."""
    sc: dict[str, Any] = {}
    data = extract_mfe_state_json(html)
    if data and isinstance(data, dict):
        d = data.get("loaderData", {}).get("data", {})
        if isinstance(d, dict) and isinstance(d.get("searchCore"), dict):
            sc = d["searchCore"]

    cat_id = str(sc.get("categoryId") or "")
    loc_id = str(sc.get("locationId") or "")

    if not cat_id or not loc_id:
        m_c = re.search(r'"categoryId":\s*"?([0-9]+)"?', html)
        m_l = re.search(r'"locationId":\s*"?([0-9]+)"?', html)
        if m_c:
            cat_id = m_c.group(1)
        if m_l:
            loc_id = m_l.group(1)

    if not cat_id or not loc_id:
        return None

    p = urllib.parse.urlparse(canonical_url)
    raw_query = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    items_params: list[tuple[str, str]] = [
        ("categoryId", cat_id),
        ("locationId", loc_id),
        ("localPriority", "0"),
        ("updateListOnly", "true"),
    ]
    if sc.get("verticalCategoryId"):
        items_params.append(("verticalCategoryId", str(sc["verticalCategoryId"])))
    if sc.get("rootCategoryId"):
        items_params.append(("rootCategoryId", str(sc["rootCategoryId"])))

    sc_params = sc.get("params") or {}
    if isinstance(sc_params, dict):
        for k, v in sc_params.items():
            items_params.append((f"params[{k}]", str(v)))

    for k, v in raw_query:
        if k != "p":
            items_params.append((k, v))

    scheme = p.scheme or "https"
    netloc = p.netloc or "www.avito.ru"
    return f"{scheme}://{netloc}/web/1/js/items?{urllib.parse.urlencode(items_params)}"


class CatalogUrlResolver:
    """Maintains mapping between canonical URLs and lightweight /web/1/js/items polling targets."""

    def __init__(self, enabled: bool | None = None, *, preserve_access_routes: bool | None = None) -> None:
        if enabled is None:
            self.enabled = os.environ.get("HUNTER_USE_JSON_POLLING", "1").lower() in ("1", "true", "yes")
        else:
            self.enabled = enabled
        self.preserve_access_routes = (
            os.environ.get("HUNTER_PRESERVE_JSON_ROUTES", "0").lower() in ("1", "true", "yes")
            if preserve_access_routes is None else preserve_access_routes
        )
        self._cache: dict[str, str] = dict(_SEED_ITEMS_URLS)

    def resolve_poll_target(self, canonical_url: str) -> tuple[str, dict[str, str]]:
        if not self.enabled:
            return canonical_url, {}
        items_url = self._cache.get(canonical_url)
        if items_url:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Referer": canonical_url,
                "X-Requested-With": "XMLHttpRequest",
                "X-Source": "client-browser",
                "Content-Type": "application/json",
            }
            return items_url, headers
        return canonical_url, {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

    def record_poll_response(self, canonical_url: str, response_text: str, status_code: int) -> str | None:
        if not self.enabled:
            return None
        if status_code in (404, 410) or (status_code in (403, 429) and not self.preserve_access_routes):
            self.invalidate(canonical_url)
            return None
        if status_code == 200 and ("<html" in response_text.lower() or "data-mfe-state" in response_text):
            from hunter.transport.session_warmup import is_embedded_redirect_or_shell
            if is_embedded_redirect_or_shell(response_text):
                return None
            classified = classify_response(status_code, response_text, canonical_url)
            if classified.outcome in (
                ResponseOutcome.CHALLENGE, ResponseOutcome.BLOCKED, ResponseOutcome.AUTH_REQUIRED,
            ):
                return None
            items_url = build_items_url_from_html(canonical_url, response_text)
            if items_url:
                self._cache[canonical_url] = items_url
                logger.info("Mapped canonical search URL to JSON items endpoint: {}", items_url[:80])
                return items_url
        return None

    def invalidate(self, canonical_url: str) -> None:
        self._cache.pop(canonical_url, None)

    def invalidate_ambiguous(self, canonical_url: str) -> None:
        if not self.preserve_access_routes:
            self.invalidate(canonical_url)


_DEFAULT_RESOLVER = CatalogUrlResolver()


def resolve_poll_target(canonical_url: str) -> tuple[str, dict[str, str]]:
    return _DEFAULT_RESOLVER.resolve_poll_target(canonical_url)


def record_poll_response(canonical_url: str, response_text: str, status_code: int) -> str | None:
    return _DEFAULT_RESOLVER.record_poll_response(canonical_url, response_text, status_code)


def invalidate_poll_target(canonical_url: str) -> None:
    _DEFAULT_RESOLVER.invalidate(canonical_url)


def invalidate_ambiguous_poll_target(canonical_url: str) -> None:
    _DEFAULT_RESOLVER.invalidate_ambiguous(canonical_url)


def to_wire_poll_url(url: str, *, prefer_gallery: bool | None = None) -> str:
    """Decorates HTML catalog poll URLs with view=gallery to reduce payload size by ~23%."""
    if prefer_gallery is None:
        prefer_gallery = os.environ.get("HUNTER_USE_GALLERY_VIEW", "0").lower() in ("1", "true", "yes")
    if not prefer_gallery or "/web/1/js/items" in url or "view=" in url:
        return url
    delimiter = "&" if "?" in url else "?"
    return f"{url}{delimiter}view=gallery"


def _fetcher_accepts_headers(fetch_fn: Any) -> bool:
    try:
        sig = inspect.signature(fetch_fn)
        params = sig.parameters
        return "headers" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (ValueError, TypeError):
        code = getattr(fetch_fn, "__code__", None)
        if code is not None:
            return "headers" in code.co_varnames
        return True


async def safe_fetch(fetcher: Any, url: str, headers: dict[str, str] | None = None) -> Any:
    """Invokes fetcher.fetch adapting signature beforehand so internal TypeErrors are not masked."""
    fetch_fn = getattr(fetcher, "fetch", None)
    if fetch_fn is None:
        raise AttributeError(f"{fetcher!r} object has no attribute 'fetch'")
    if _fetcher_accepts_headers(fetch_fn):
        return await fetch_fn(url, headers=headers)
    return await fetch_fn(url)


async def warmup_client_session(
    client: Any, html_url: str, endpoint: str | None = None, proxy_credentials: Any = None,
    executor: Any = None,
) -> bool:
    """Warms up an independent client session by visiting HTML catalog page and solving PoW if needed."""
    from hunter.transport.session_warmup import perform_session_warmup
    result = await perform_session_warmup(
        client, html_url, endpoint=endpoint, proxy_credentials=proxy_credentials, executor=executor
    )
    return result.is_ready

