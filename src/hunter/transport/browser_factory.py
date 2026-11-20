"""Camoufox anti-detect browser factory for session warmup and challenge solving."""
from __future__ import annotations

import asyncio
import glob
import inspect
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from typing import Any
from loguru import logger

from hunter.domain.slots import SlotSnapshot
from hunter.transport.proxy_credentials import ProxyCredentials
from hunter.transport.proxy_relay import LocalProxyRelay
from hunter.transport.session_store import SessionStore


def _prune_stale_profiles(max_age_sec: float = 180.0) -> None:
    now = time.time()
    dirs_to_check = {tempfile.gettempdir(), "/tmp"}
    tmpdir_env = os.environ.get("TMPDIR")
    if tmpdir_env:
        dirs_to_check.add(tmpdir_env)
    for base_dir in dirs_to_check:
        if not os.path.isdir(base_dir):
            continue
        for pattern in ("playwright_*", "camoufox_*"):
            for path in glob.glob(os.path.join(base_dir, pattern)):
                try:
                    if now - os.stat(path).st_mtime >= max_age_sec:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                except Exception:
                    pass


async def _prefill_slot_cookies(context: Any, slot: SlotSnapshot, session_store: SessionStore | None) -> None:
    if session_store is None:
        return
    try:
        sess = await session_store.load_session(slot.slot_id, expected_endpoint=slot.endpoint or "")
        if sess and sess.cookies:
            await context.add_cookies(sess.cookies)
    except Exception as exc:
        logger.debug("Could not pre-load cookies into challenge browser: {}", exc)


async def _poll_for_pow_tokens(context: Any, timeout_sec: float = 12.0) -> list[dict[str, Any]]:
    start = time.monotonic()
    while time.monotonic() - start < timeout_sec:
        cookies = await context.cookies()
        names = {c.get("name") for c in cookies if isinstance(c, dict)}
        if ("_adcc" in names or "pow_solved" in names) and (
            "ft" in names or "f" in names or "u" in names or "srv_id" in names
        ):
            return [c for c in cookies if isinstance(c, dict) and c.get("name") and c.get("value")]
        await asyncio.sleep(0.5)
    return [c for c in await context.cookies() if isinstance(c, dict) and c.get("name") and c.get("value")]


async def _setup_route_blocking(page: Any) -> None:
    async def _route_blocker(route: Any) -> None:
        url = route.request.url
        if route.request.resource_type in {"media"} or (
            route.request.resource_type == "image" and "/web/1/u" not in url and not url.startswith("https://www.avito.ru")
        ):
            await route.abort()
        else:
            await route.continue_()

    try:
        await page.route("**/*", _route_blocker)
    except Exception as exc:
        logger.debug("Could not attach route blocker: {}", exc)


async def _browser_proxy(
    endpoint: str | None, credentials: ProxyCredentials | None = None
) -> tuple[dict[str, str] | None, Any]:
    """Builds the Playwright proxy dict.

    Playwright's own proxy credentials keep Qrator from issuing the ``ft``
    cookie, so authenticated proxies are reached through a loopback relay.
    """
    if not endpoint or endpoint in {"direct", "direct://"}:
        return None, None
    if credentials is None:
        return {"server": endpoint}, None
    relay = LocalProxyRelay(endpoint, credentials)
    return {"server": await relay.start()}, relay


def _context_options(settings: Any) -> dict[str, Any]:
    """RU locale, Moscow timezone and a desktop viewport are required for ft."""
    return {
        "locale": "ru-RU",
        "timezone_id": getattr(settings, "timezone", None) or "Europe/Moscow",
        "viewport": {"width": 1366, "height": 820},
    }


async def _stop_relay(relay: Any) -> None:
    if relay is None:
        return
    try:
        await asyncio.wait_for(relay.stop(), timeout=4.0)
    except Exception as exc:
        logger.debug("Could not stop proxy relay: {}", exc)


async def _mask_webdriver(context: Any) -> None:
    """Hides the Playwright automation marker without creating an enumerable own-property."""
    try:
        await context.add_init_script(
            "delete Object.getPrototypeOf(navigator).webdriver; delete Navigator.prototype.webdriver;"
        )
    except Exception as exc:
        logger.debug("Could not mask webdriver: {}", exc)


async def _needs_fingerprint_warmup(context: Any, page: Any) -> bool:
    """Detects the Qrator IP block page that never issues the ft fingerprint cookie."""
    try:
        cookies = await context.cookies()
    except Exception:
        cookies = []
    if any(c.get("name") == "ft" and c.get("value") for c in cookies if isinstance(c, dict)):
        return False
    try:
        title = (await page.title()).lower()
    except Exception:
        return False
    return "проблема с ip" in title or "доступ ограничен" in title


async def _warmup_fingerprint(page: Any, base_url: str, timeout_sec: float = 12.0) -> bool:
    """Visits the main catalog page that runs the fingerprint JS and issues f/ft cookies."""
    warm_url = base_url.rstrip("/") + "/"
    try:
        await page.goto(warm_url, wait_until="domcontentloaded", timeout=15000)
    except Exception as exc:
        logger.debug("Fingerprint warmup navigation failed: {}", exc)
        return False
    start = time.monotonic()
    while time.monotonic() - start < timeout_sec:
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False
        if any(c.get("name") == "ft" and c.get("value") for c in cookies if isinstance(c, dict)):
            return True
        await asyncio.sleep(0.5)
    return False


async def _extract_session_data(page: Any, context: Any) -> tuple[list[dict[str, Any]], str, str]:
    cookies = await _poll_for_pow_tokens(context, timeout_sec=12.0)
    try:
        ua = await page.evaluate("() => navigator.userAgent")
    except Exception:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
    try:
        lang = await page.evaluate("() => navigator.language")
    except Exception:
        lang = "ru-RU"
    return cookies, ua, lang


async def _save_warmed_session(
    slot: SlotSnapshot,
    session_store: SessionStore | None,
    cookies: list[dict[str, Any]],
    ua: str,
    lang: str,
) -> None:
    if not session_store:
        return
    try:
        await session_store.save_session(
            slot_id=slot.slot_id,
            endpoint=slot.endpoint or "",
            version=1,
            generation=slot.generation,
            cookies=cookies,
            storage_state={
                "user_agent": ua,
                "accept_language": f"{lang},ru;q=0.8,en-US;q=0.5,en;q=0.3",
            },
            force=True,
        )
    except Exception as exc:
        logger.warning("Could not persist warmed session for slot {}: {}", slot.slot_id, exc)


async def _close_browser(browser: Any) -> None:
    try:
        await asyncio.wait_for(browser.close(), timeout=4.0)
    except Exception:
        pass


async def _aexit_camoufox(cm: Any) -> None:
    try:
        await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=4.0)
    except Exception:
        pass


async def _stop_playwright(playwright: Any) -> None:
    try:
        await asyncio.wait_for(playwright.stop(), timeout=4.0)
    except Exception:
        pass


async def _launch_camoufox(
    proxy: dict[str, str] | None,
    relay: Any,
    context_options: dict[str, Any],
    headless_mode: Any,
) -> tuple[Any, Any, Callable[[], Awaitable[None]]]:
    from camoufox.async_api import AsyncCamoufox

    from hunter.transport.camoufox_setup import camoufox_options

    cm = AsyncCamoufox(**camoufox_options(headless_mode, proxy))
    browser = await cm.__aenter__()
    try:
        context = await browser.new_context(**context_options)
        await _mask_webdriver(context)
    except BaseException:
        await _close_browser(browser)
        await _aexit_camoufox(cm)
        raise

    async def _close() -> None:
        await _close_browser(browser)
        await _aexit_camoufox(cm)
        await _stop_relay(relay)
        _prune_stale_profiles(max_age_sec=30.0)

    return browser, context, _close


async def _launch_chromium(
    proxy: dict[str, str] | None,
    relay: Any,
    context_options: dict[str, Any],
    has_display: bool,
) -> tuple[Any, Any, Callable[[], Awaitable[None]]]:
    from playwright.async_api import async_playwright

    p = await async_playwright().start()
    launch_kwargs: dict[str, Any] = {
        "headless": not has_display,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    if proxy:
        launch_kwargs["proxy"] = proxy
    browser = None
    try:
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(**context_options)
        await _mask_webdriver(context)
    except BaseException:
        if browser is not None:
            await _close_browser(browser)
        await _stop_playwright(p)
        await _stop_relay(relay)
        raise

    async def _close() -> None:
        await _close_browser(browser)
        await _stop_playwright(p)
        await _stop_relay(relay)
        _prune_stale_profiles(max_age_sec=30.0)

    return browser, context, _close


async def _launch_browser_context(
    slot: SlotSnapshot,
    credentials: ProxyCredentials | None = None,
    settings: Any = None,
) -> tuple[Any, Any, Callable[[], Awaitable[None]]]:
    _prune_stale_profiles(max_age_sec=180.0)
    proxy, relay = await _browser_proxy(slot.endpoint, credentials)
    context_options = _context_options(settings)
    has_display = bool(os.environ.get("DISPLAY"))
    headless_mode = False if has_display else ("virtual" if shutil.which("Xvfb") else True)
    try:
        return await _launch_camoufox(proxy, relay, context_options, headless_mode)
    except asyncio.CancelledError:
        await _stop_relay(relay)
        raise
    except Exception as exc:
        logger.warning(
            "Camoufox launch failed ({}), falling back to chromium: {}", type(exc).__name__, exc
        )
        return await _launch_chromium(proxy, relay, context_options, has_display)


async def _prepare_slot_page(page: Any, base_url: str) -> None:
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        if await _needs_fingerprint_warmup(page.context, page):
            warmed = await _warmup_fingerprint(page, base_url)
            logger.info("Qrator fingerprint warmup for slot page: solved={}", warmed)
            if warmed:
                await page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
        title = await page.title()
        if any(k in title.lower() for k in ("проверка безопасности", "firewall", "доступ ограничен")):
            from hunter.collect.firewall_pow import solve_firewall_pow
            await solve_firewall_pow(page)
            await asyncio.sleep(2.0)
    except Exception as exc:
        logger.warning("Could not open slot page {}: {}", base_url, exc)


async def _open_slot_page(
    slot: SlotSnapshot,
    settings: Any,
    session_store: SessionStore | None,
    credentials: ProxyCredentials | None = None,
) -> Any:
    browser, context, close_browser = await _launch_browser_context(slot, credentials, settings)
    try:
        await _prefill_slot_cookies(context, slot, session_store)

        page = await context.new_page()
        base_url = getattr(settings, "avito_base_url", None) or "https://www.avito.ru"
        await _prepare_slot_page(page, base_url)
        await _setup_route_blocking(page)

        cookies, ua, lang = await _extract_session_data(page, context)
        await _save_warmed_session(slot, session_store, cookies, ua, lang)

        closed = False
        orig_page_close = page.close
        async def _safe_close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                await asyncio.wait_for(orig_page_close(), timeout=3.0)
            except Exception:
                pass
            await close_browser()

        page.close = _safe_close
        return page
    except BaseException:
        await close_browser()
        raise


def create_slot_browser_factory(
    settings: Any,
    session_store: SessionStore | None = None,
    credential_store: Any = None,
) -> Any:
    """Returns a BrowserContextFactory callable for ChallengeHandoffService."""
    async def _factory(slot: SlotSnapshot) -> Any:
        credentials = None
        if credential_store is not None and slot.endpoint not in {"", "direct", "direct://"}:
            credentials = credential_store.load(slot.slot_id, slot.endpoint)
        return await _open_slot_page(slot, settings, session_store, credentials)

    def bind_session_store(store: SessionStore) -> None:
        nonlocal session_store
        session_store = store

    def bind_credential_store(store: Any) -> None:
        nonlocal credential_store
        credential_store = store

    _factory.bind_session_store = bind_session_store
    _factory.bind_credential_store = bind_credential_store
    return _factory
