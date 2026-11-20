from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any

from loguru import logger

from hunter.core.config import get_settings
from hunter.collect.captcha_pause import looks_like_captcha
from playwright.async_api import TimeoutError as PlaywrightTimeout


async def perform_initial_navigation_and_captcha_check(page: Any) -> None:
    try:
        await page.goto("https://www.avito.ru", wait_until="domcontentloaded", timeout=30_000)
        html = await page.content()
        if looks_like_captcha(html, page.url):
            logger.warning(
                "Капча или проверка на старте! Ожидание автоматического прохождения (до 15 сек)..."
            )
            for _ in range(5):
                await asyncio.sleep(3)
                try:
                    html = await page.content()
                    if not looks_like_captcha(html, page.url):
                        logger.info("Проверка пройдена, браузер готов к работе!")
                        break
                except Exception:
                    break
            else:
                logger.warning(
                    "Защитная проверка не была снята на старте. "
                    "Оркестратор перейдет в режим паузы/cooldown при необходимости."
                )
    except Exception as exc:
        logger.debug("initial navigation to avito.ru: {}", exc)


async def init_session_page(context: Any) -> tuple[Any, int]:
    page = context.pages[0] if (context and context.pages) else await context.new_page()
    await perform_initial_navigation_and_captcha_check(page)
    return page, 0


def _cleanup_profile_preferences(profile_dir: Path) -> None:
    try:
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = profile_dir / lock_name
            if lock_path.is_symlink() or lock_path.exists():
                lock_path.unlink(missing_ok=True)
        pref_file = profile_dir / "Default" / "Preferences"
        if pref_file.exists():
            content = pref_file.read_text(encoding="utf-8")
            new_content = re.sub(r'"exit_type"\s*:\s*"[^"]*"', '"exit_type":"Normal"', content)
            if new_content != content:
                pref_file.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        logger.debug("Error updating profile preferences on close: {}", exc)


async def close_browser_session_db(session: Any) -> None:
    await session.save_profile_states()
    session._page = None
    session._nav_count = 0
    session._light_page = None
    session._light_nav_count = 0
    for slot in session.profiles[1:]:
        if slot.context is not None:
            try:
                await slot.context.close()
            except Exception as exc:
                logger.debug("Error closing secondary profile {}: {}", slot.profile_id, exc)
    session.profiles.clear()
    if session.context:
        try:
            await session.context.close()
        except Exception as exc:
            logger.debug("Error closing browser context: {}", exc)
        session.context = None
    if session._playwright:
        try:
            await session._playwright.stop()
        except Exception as exc:
            logger.debug("Error stopping playwright: {}", exc)
        session._playwright = None

    try:
        settings = get_settings()
        _cleanup_profile_preferences(settings.profile_dir)
    except Exception as exc:
        logger.debug("Error getting settings on close: {}", exc)


async def restart_browser_session_db(session: Any) -> None:
    """Cleanly restart browser context and playwright driver to free process memory."""
    await session.close()
    await session.start()


async def prepare_fetch_page(session: Any) -> Any:
    session._nav_count += 1
    if session._nav_count > 20:
        return await session._safe_recycle_tab()
    return await session._get_or_create_page()


async def _wait_for_grid_and_price(page: Any, url: str) -> None:
    grid_selector = (
        "[data-marker='item'], [data-marker='empty-page'], "
        "[class*='empty-page'], [class*='no-results'], "
        "[data-marker*='item-view'], [data-marker*='seller-info']"
    )
    try:
        await page.wait_for_selector(grid_selector, timeout=15_000)
        await asyncio.sleep(0.4)
    except PlaywrightTimeout:
        logger.debug("Selector wait timed out on {}, falling back to networkidle 5s", url)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeout:
            pass

    settings = get_settings()
    b_cfg = settings.browser or {}
    if b_cfg.get("price_wait_enabled", False):
        price_timeout_sec = float(b_cfg.get("price_selector_timeout_sec", 6.0))
        try:
            await page.evaluate("window.scrollBy(0, 350)")
            await page.wait_for_selector(
                "[data-marker='item-price'], [itemprop='price']",
                timeout=int(price_timeout_sec * 1000),
            )
            await page.evaluate("window.scrollBy(0, 450)")
            await asyncio.sleep(0.3)
        except PlaywrightTimeout:
            logger.debug("Price selector wait timed out after {}s on {}", price_timeout_sec, url)
        except Exception as exc:
            logger.debug("Stepped scroll/wait on {}: {}", url, exc)


async def execute_page_fetch_navigation(page: Any, url: str) -> tuple[int, str, str]:
    res = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    cur_status = res.status if res else 0

    await _wait_for_grid_and_price(page, url)

    cur_html = await page.content()
    cur_final_url = page.url

    if (
        cur_status in (429, 439)
        or "firewall" in cur_html.lower()
        or "firewallpow" in cur_html.lower()
        or "/firewall" in cur_final_url.lower()
    ):
        logger.info(
            'HTTP {} / firewall challenge in browser.fetch on {}. Solving '
            'firewallPoW...', cur_status, url)
        from hunter.collect.firewall_pow import solve_firewall_pow

        solved = await solve_firewall_pow(page, html=cur_html)
        if solved:
            logger.info("firewallPoW unblock verified! Re-navigating to {}", url)
            res = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            cur_status = res.status if res else 0
            cur_html = await page.content()
            cur_final_url = page.url

    return cur_status, cur_html, cur_final_url
