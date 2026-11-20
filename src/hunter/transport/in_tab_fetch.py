"""In-tab Playwright browser fetcher for executing DOM-level fetch with JS PoW evaluation."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any
from loguru import logger

from hunter.collect.lightfetch import V1_FETCH_SCRIPT
from hunter.collect.firewall_pow import solve_firewall_pow
from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.http_fetch import SlotFetchResponse


async def _safe_close_page(page: Any | None) -> None:
    if page is not None:
        try:
            await asyncio.wait_for(page.close(), timeout=3.0)
        except Exception:
            pass


async def _solve_pow_and_retry(page: Any, url: str, timeout_sec: float, html: str = "") -> dict[str, Any] | None:
    try:
        solved = await solve_firewall_pow(page, html=html)
        if solved:
            await asyncio.sleep(0.5)
            for _ in range(3):
                try:
                    return await asyncio.wait_for(page.evaluate(V1_FETCH_SCRIPT, url), timeout=timeout_sec)
                except Exception as e:
                    if "Execution context was destroyed" in str(e):
                        await asyncio.sleep(1.0)
                        continue
                    raise
    except Exception as exc:
        logger.debug("In-tab PoW solve error: {}", exc)
    return None


class InTabSlotFetcher:
    """Performs in-browser fetch() calls inside a persistent Playwright same-origin tab."""

    def __init__(
        self,
        slot_id: str,
        endpoint: str | None = None,
        browser_factory: Any | None = None,
        timeout_sec: float = 15.0,
        session_store: Any | None = None,
    ) -> None:
        self.slot_id = slot_id
        self.endpoint = endpoint
        self.browser_factory = browser_factory
        self.timeout_sec = timeout_sec
        self.session_store = session_store
        self._page: Any | None = None
        self._lock = asyncio.Lock()
        self._nav_count: int = 0
        self.identity_agnostic: bool = True

    async def _ensure_origin(self, page: Any) -> None:
        """Navigates unnavigated about:blank tabs to https://www.avito.ru/ to establish valid origin."""
        cur = getattr(page, "url", "") or ""
        if not cur or cur.startswith("about:"):
            try:
                await page.goto("https://www.avito.ru/", timeout=self.timeout_sec * 1000)
            except Exception as exc:
                logger.debug("Failed establishing origin in _ensure_origin: {}", exc)

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            is_closed = getattr(self._page, "is_closed", None)
            if callable(is_closed) and not is_closed():
                return self._page
            self._page = None
        if not self.browser_factory:
            raise RuntimeError("Browser factory is required for in-tab fetch")
        snapshot = SlotSnapshot(
            slot_id=self.slot_id,
            endpoint=self.endpoint or "direct",
            role=SlotRole.ACTIVE,
            health=SlotHealth.READY,
            session_version=1,
            generation=1,
            updated_at="",
        )
        self._page = await self.browser_factory(snapshot)
        if self.session_store:
            try:
                payload = await self.session_store.load_session(self.slot_id, expected_endpoint=self.endpoint or "")
                if payload and payload.cookies:
                    ctx = getattr(self._page, "context", None)
                    if ctx and hasattr(ctx, "add_cookies"):
                        await ctx.add_cookies(payload.cookies)
            except Exception as exc:
                logger.debug("Failed prefilling cookies in _ensure_page: {}", exc)
        self._nav_count = 0
        return self._page

    async def fetch(self, url: str) -> SlotFetchResponse:
        """Executes fetch() inside persistent Chromium tab and returns SlotFetchResponse."""
        t0 = time.perf_counter()
        async with self._lock:
            try:
                page = await self._ensure_page()
                await self._ensure_origin(page)
                raw = await asyncio.wait_for(
                    page.evaluate(V1_FETCH_SCRIPT, url),
                    timeout=self.timeout_sec,
                )
                status = int(raw.get("status", 0)) if isinstance(raw, dict) else 0
                text = str(raw.get("text", "") if isinstance(raw, dict) else "")
                final_url = str(raw.get("url", url) if isinstance(raw, dict) else url)
                err = raw.get("error") if isinstance(raw, dict) else "Invalid response"

                if status == 439 or "firewallPow" in text or "проверка безопасности" in text:
                    retry = await _solve_pow_and_retry(page, url, self.timeout_sec, html=text)
                    if retry and isinstance(retry, dict):
                        status = int(retry.get("status", status))
                        text = str(retry.get("text", text))
                        final_url = str(retry.get("url", final_url))
                        err = retry.get("error")

                self._nav_count += 1
                if self._nav_count % 25 == 0:
                    try:
                        await page.evaluate("() => { if (window.gc) window.gc(); }")
                    except Exception:
                        pass

                elapsed = time.perf_counter() - t0
                return SlotFetchResponse(
                    status_code=status,
                    url=final_url,
                    text=text,
                    elapsed_sec=elapsed,
                    error=err,
                    wire_body_bytes=len(text.encode("utf-8")),
                )
            except TimeoutError:
                return SlotFetchResponse(
                    status_code=0, url=url, text="",
                    elapsed_sec=time.perf_counter() - t0, error="timeout",
                )
            except Exception as exc:
                err_str = str(exc)
                if any(k in err_str for k in ("Target page, context or browser has been closed", "Session closed")):
                    await _safe_close_page(self._page)
                    self._page = None
                return SlotFetchResponse(
                    status_code=0, url=url, text="",
                    elapsed_sec=time.perf_counter() - t0, error=err_str,
                )

    async def load_cookies_from_store(self) -> int:
        """Loads session cookies if store is configured; persistent browser maintains its own cookies."""
        if not self.session_store:
            return 0
        try:
            payload = await self.session_store.load_session(
                self.slot_id, expected_endpoint=self.endpoint or ""
            )
            if payload and payload.cookies:
                page = await self._ensure_page()
                context = getattr(page, "context", None)
                if context and hasattr(context, "add_cookies"):
                    await context.add_cookies(payload.cookies)
                return len(payload.cookies)
        except Exception as exc:
            logger.debug("Failed loading cookies in InTabSlotFetcher: {}", exc)
        return 0

    async def save_cookies_to_store(self, version: int = 1, generation: int = 1) -> None:
        """Saves browser context cookies into session store if configured."""
        if not self.session_store:
            return
        try:
            page = await self._ensure_page()
            context = getattr(page, "context", None)
            if context and hasattr(context, "cookies"):
                cookies = await context.cookies()
                await self.session_store.save_session(
                    slot_id=self.slot_id,
                    endpoint=self.endpoint or "",
                    version=version,
                    generation=generation,
                    cookies=cookies,
                )
        except Exception as exc:
            logger.debug("Failed saving cookies in InTabSlotFetcher: {}", exc)

    async def close(self) -> None:
        async with self._lock:
            if self._page is not None:
                p = self._page
                self._page = None
                try:
                    close_fn = getattr(p, "close", None)
                    if close_fn:
                        await close_fn() if inspect.iscoroutinefunction(close_fn) else close_fn()
                except Exception:
                    pass
