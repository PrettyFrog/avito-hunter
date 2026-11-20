"""Session warmup and readiness validation for public transport slots (T-968)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger
from hunter.transport.attempt_scope import scoped_http_call
from hunter.transport.metered_http import HttpDeferred

from hunter.transport.classification import CHALLENGE_MARKERS, _is_empty_json, _is_empty_mfe


class WarmupOutcome(str, Enum):
    READY = "ready"
    CHALLENGE_FAILED = "challenge_failed"
    EMBEDDED_REDIRECT = "embedded_redirect"
    HTTP_ERROR = "http_error"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True, slots=True)
class WarmupResult:
    outcome: WarmupOutcome
    is_ready: bool
    details: str = ""

    def __bool__(self) -> bool:
        return self.is_ready


_EMBEDDED_REDIRECT_PATTERNS = (
    re.compile(r'<meta\s+http-equiv=["\']?refresh["\']?', re.IGNORECASE),
    re.compile(r'window\.location(?:\.replace|\.href)?\s*=', re.IGNORECASE),
    re.compile(r'location\.(?:replace|href)\s*=', re.IGNORECASE),
    re.compile(r'["\']redirectUrl["\']\s*:', re.IGNORECASE),
    re.compile(r'["\']redirect_url["\']\s*:', re.IGNORECASE),
    re.compile(r'["\']redirect["\']\s*:\s*["\']', re.IGNORECASE),
    re.compile(r'["\']isSuspenseRedirect["\']\s*:\s*true', re.IGNORECASE),
)


def is_embedded_redirect_or_shell(html: str) -> bool:
    """Detects HTML shells with client-side meta/script redirects or empty bodies."""
    stripped = html.strip()
    if not stripped:
        return True
    for pattern in _EMBEDDED_REDIRECT_PATTERNS:
        if pattern.search(html):
            return True
    text_only = re.sub(r"<[^>]+>", "", html).strip()
    return not text_only and "__initialData__" not in html and "data-marker" not in html


_INITIAL_DATA_RE = re.compile(
    r'(?:window\.__initialData__|window\.__state__)\s*=\s*(\{.*?\})\s*(?:;|</script>)',
    re.DOTALL,
)
_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)
_EMPTY_HTML_MARKERS = (
    'data-marker="catalog-empty"',
    "ничего не найдено",
    "объявлений пока нет",
    "похожие объявления рядом",
)
_CATALOG_HTML_MARKERS = (
    'data-marker="item"',
    'data-marker="catalog"',
    'data-marker="catalog-serp"',
    'class="iva-item-root',
    "item-title",
)


def _is_valid_catalog_json_structure(data: Any) -> bool:
    if isinstance(data, list):
        return not data or all(isinstance(x, dict) for x in data)
    if not isinstance(data, dict):
        return False
    if data.get("status") in ("error", "failed", "fail") or "error" in data or "errors" in data:
        return False
    for k in ("result", "data"):
        val = data.get(k)
        if isinstance(val, dict) and (
            val.get("status") in ("error", "failed", "fail") or "error" in val or "errors" in val
        ):
            return False
    if _is_empty_json(data) or _is_empty_mfe(data):
        return True
    for k in ("result", "data"):
        val = data.get(k)
        if isinstance(val, dict) and (_is_empty_json(val) or _is_empty_mfe(val)):
            return True
    loader = data.get("loaderData")
    if isinstance(loader, dict):
        if loader.get("redirect") or loader.get("isSuspenseRedirect"):
            return False
        sub = loader.get("data")
        if isinstance(sub, dict) and _is_valid_catalog_json_structure(sub):
            return True
    items = data.get("items")
    if items is None:
        for container in (data.get("catalog"), data.get("result"), data.get("data")):
            if isinstance(container, dict):
                sub_items = container.get("items")
                if sub_items is not None:
                    items = sub_items
                    break
                sub_cat = container.get("catalog")
                if isinstance(sub_cat, dict) and sub_cat.get("items") is not None:
                    items = sub_cat.get("items")
                    break
    if items is not None:
        return isinstance(items, list)
    return False


def has_catalog_structure(text: str) -> bool:
    """Validates presence of confirmed catalog items, MFE state, or JSON structures."""
    stripped = text.strip()
    if not stripped:
        return False
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            return _is_valid_catalog_json_structure(json.loads(stripped))
        except Exception:
            return False
    lower = text.lower()
    if any(m in lower for m in _EMPTY_HTML_MARKERS) or any(m in text for m in _CATALOG_HTML_MARKERS):
        return True
    if "data-mfe-state" in text:
        try:
            from hunter.collect.parsers.mfe_state import extract_mfe_state_json
            mfe_json = extract_mfe_state_json(text)
            if mfe_json and (_is_valid_catalog_json_structure(mfe_json) or _is_empty_mfe(mfe_json)):
                return True
        except Exception:
            pass
    for match in _INITIAL_DATA_RE.finditer(text):
        try:
            if _is_valid_catalog_json_structure(json.loads(match.group(1))):
                return True
        except Exception:
            pass
    for match in _SCRIPT_JSON_RE.finditer(text):
        try:
            if _is_valid_catalog_json_structure(json.loads(match.group(1))):
                return True
        except Exception:
            pass
    return False



def is_challenge_page(status_code: int, html: str) -> bool:
    """Detects active challenge or PoW barrier markers in response."""
    if status_code in (429, 439):
        return True
    html_lower = html.lower()
    return any(marker.lower() in html_lower for marker in CHALLENGE_MARKERS)


async def _extract_response_text(r: Any) -> str:
    text = getattr(r, "text", None)
    if text is not None:
        return text or ""
    if hasattr(r, "aiter_content"):
        from hunter.transport.http_body_reader import read_response_content
        _, text = await read_response_content(r, 1024 * 1024)
        return text or ""
    if hasattr(r, "content"):
        c = getattr(r, "content", b"")
        return c.decode("utf-8", errors="replace") if isinstance(c, bytes) else str(c)
    return ""


async def _ensure_warmup_ft_token(client: Any) -> None:
    c_obj = getattr(client, "cookies", None)
    if bool(c_obj and ("ft" in c_obj or (hasattr(c_obj, "get") and c_obj.get("ft")))):
        return
    from hunter.transport.qrator_handshake import perform_qrator_handshake
    result = await perform_qrator_handshake(client, slot_id="warmup", identity="user_a", purpose="warmup", timeout_sec=10.0)
    if not result.is_success:
        raise RuntimeError(f"Warmup handshake failed: {result.outcome.value}")


async def refresh_expired_ft(client: Any, acquired_at: float | None = None) -> bool:
    """Refreshes ft cookie if expired based on assumed 18h TTL."""
    from hunter.transport.qrator_handshake import is_ft_expired, perform_qrator_handshake
    if is_ft_expired(acquired_at):
        res = await perform_qrator_handshake(client, slot_id="refresh", identity="user_a", purpose="ft_refresh")
        return bool(res.is_success)
    return True


async def perform_session_warmup(
    client: Any, html_url: str, endpoint: str | None = None, proxy_credentials: Any = None,
    executor: Any = None,
) -> WarmupResult:
    """Performs HTTP-based session warmup with challenge validation."""
    if not client or not hasattr(client, "get"):
        return WarmupResult(outcome=WarmupOutcome.NOT_ATTEMPTED, is_ready=False, details="No client")
    doc_hdrs = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1", "Referer": "https://www.avito.ru/",
    }
    try:
        r = await scoped_http_call(client, "warmup", "get", html_url, headers=doc_hdrs, timeout=15.0)
        status = getattr(r, "status_code", None)
        text = await _extract_response_text(r)
        if status == 439 or is_challenge_page(status or 0, text):
            from hunter.collect.firewall_pow import extract_pow_challenge, solve_firewall_pow_http
            token = extract_pow_challenge(text, cookies=dict(getattr(r, "cookies", {}) or {}))
            if not token:
                return WarmupResult(outcome=WarmupOutcome.CHALLENGE_FAILED, is_ready=False, details="Challenge token missing")
            ua = getattr(client, "headers", {}).get("User-Agent", "")
            ok, cookies = await solve_firewall_pow_http(
                token,
                cookies=dict(getattr(r, "cookies", {}) or {}),
                headers={"User-Agent": ua},
                http_client=client,
                endpoint=endpoint,
                proxy_credentials=proxy_credentials,
                executor=executor,
            )
            if not ok:
                return WarmupResult(outcome=WarmupOutcome.CHALLENGE_FAILED, is_ready=False, details="PoW solver failed")
            if hasattr(client, "cookies"):
                for c in cookies:
                    client.cookies.set(c["name"], c["value"], domain=c.get("domain", ".avito.ru"), path=c.get("path", "/"))
            await asyncio.sleep(1.0)
            r = await scoped_http_call(client, "warmup", "get", html_url, headers={"Accept": "text/html", "Referer": "https://www.avito.ru/"}, timeout=15.0)
            status = getattr(r, "status_code", None)
            text = await _extract_response_text(r)

        if status != 200:
            return WarmupResult(outcome=WarmupOutcome.HTTP_ERROR, is_ready=False, details=f"HTTP {status}")
        if is_embedded_redirect_or_shell(text):
            return WarmupResult(outcome=WarmupOutcome.EMBEDDED_REDIRECT, is_ready=False, details="Embedded redirect")
        if is_challenge_page(status, text):
            return WarmupResult(outcome=WarmupOutcome.CHALLENGE_FAILED, is_ready=False, details="Challenge in 200 body")
        if not has_catalog_structure(text):
            return WarmupResult(outcome=WarmupOutcome.EMBEDDED_REDIRECT, is_ready=False, details="Missing confirmed catalog structure")

        await _ensure_warmup_ft_token(client)

        return WarmupResult(outcome=WarmupOutcome.READY, is_ready=True, details="Session ready")
    except HttpDeferred:
        raise
    except Exception as exc:
        logger.debug("Warmup of client session failed: {}", exc)
        return WarmupResult(outcome=WarmupOutcome.HTTP_ERROR, is_ready=False, details=str(exc))
