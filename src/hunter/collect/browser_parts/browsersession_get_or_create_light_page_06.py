from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from hunter.core.config import get_settings
from hunter.collect.browser_parts.is_target_closed_error_01 import (
    ProfileSlot,
    is_context_closed,
    is_target_closed_error,
)


async def _navigate_and_assign_light_page(session: Any, slot: ProfileSlot) -> Any:
    new_page = await slot.context.new_page()
    await new_page.goto(
        'https://www.avito.ru/robots.'
        'txt', wait_until="domcontentloaded", timeout=15000)
    slot.light_page = new_page
    slot.light_nav_count = 0
    if slot.index == 0:
        session._light_page = new_page
        session._light_nav_count = 0
    return new_page


async def _handle_recycle_error(session: Any, slot: ProfileSlot, exc: Exception, profile_id: str | None) -> Any:
    if is_target_closed_error(exc):
        logger.info(
            'Target closed during light page recycle ({}), auto-restarting '
            'browser...', slot.profile_id)
        await session.restart()
        slot = session.get_profile(profile_id)
        assert slot is not None and slot.context is not None
        return await _navigate_and_assign_light_page(session, slot)

    logger.warning("Error recycling light page ({}): {}", slot.profile_id, exc)
    if getattr(slot.context, "pages", None):
        slot.light_page = slot.context.pages[0]
    else:
        try:
            slot.light_page = await slot.context.new_page()
        except Exception as np_exc:
            if is_target_closed_error(np_exc):
                await session.restart()
                slot = session.get_profile(profile_id)
                return slot.light_page or await session.get_or_create_light_page(profile_id=profile_id)
    slot.light_nav_count = 0
    if slot.index == 0:
        session._light_page = slot.light_page
        session._light_nav_count = 0
    return slot.light_page


async def get_or_create_light_page_db(session: Any, profile_id: str | None = None) -> Any:
    """Retrieve persistent same-origin tab for light in-tab fetch, recycling if necessary."""
    slot = session.get_profile(profile_id)
    assert slot is not None
    if is_context_closed(slot.context):
        logger.warning(
            'Browser context closed in get_or_create_light_page ({}). Restarting '
            'browser...', slot.profile_id)
        await session.restart()
        slot = session.get_profile(profile_id)
        assert slot is not None and slot.context is not None
    recycle_interval = 200
    try:
        settings = get_settings()
        raw_int = getattr(settings.collect, "tab_recycle_interval", 200)
        if isinstance(raw_int, int) and not isinstance(raw_int, bool):
            recycle_interval = raw_int
    except Exception:
        pass

    if slot.index == 0 and session._light_page is not None and slot.light_page is None:
        slot.light_page = session._light_page
        slot.light_nav_count = session._light_nav_count

    if slot.light_page is not None:
        try:
            nav_cnt = session._light_nav_count if (slot.index == 0 and session._light_nav_count > slot.light_nav_count) else slot.light_nav_count
            if not slot.light_page.is_closed() and nav_cnt < recycle_interval:
                return slot.light_page
        except Exception:
            pass
    return await session._recycle_light_page(profile_id=slot.profile_id)


async def recycle_light_page_db(session: Any, profile_id: str | None = None) -> Any:
    """Safely rotate light fetch tab: create and navigate new tab before closing old one."""
    slot = session.get_profile(profile_id)
    assert slot is not None
    if is_context_closed(slot.context):
        await session.restart()
        slot = session.get_profile(profile_id)
        assert slot is not None and slot.context is not None
    old_page = session._light_page if (slot.index == 0 and slot.light_page is None) else slot.light_page
    try:
        new_page = await _navigate_and_assign_light_page(session, slot)
        if old_page is not None:
            try:
                if not old_page.is_closed():
                    await old_page.close()
            except Exception as exc:
                logger.debug("Error closing old light page ({}): {}", slot.profile_id, exc)
        logger.info("Persistent light fetch tab initialized/recycled safely ({})", slot.profile_id)
        return new_page
    except Exception as exc:
        return await _handle_recycle_error(session, slot, exc, profile_id)


async def record_fetch_stats_and_gc(session: Any, slot: ProfileSlot | None, page: Any) -> None:
    if slot is not None:
        slot.light_nav_count += 1
        slot.total_requests += 1
        slot.last_request_ts = time.monotonic()
        if slot.index == 0:
            session._light_nav_count = slot.light_nav_count
        if slot.light_nav_count % 25 == 0:
            await session._force_gc(page)
    else:
        session._light_nav_count += 1
        if session._light_nav_count % 25 == 0:
            await session._force_gc(page)


async def handle_pow_challenge_if_needed(
    page: Any,
    url: str,
    status: int,
    final_url: str,
    html: str,
    error: Any,
    timeout_sec: float,
) -> tuple[int, str, str, str | None]:
    if status == 439 or "firewallPow" in html or "/firewall" in final_url.lower():
        logger.info("HTTP 439 / firewall challenge in evaluate_in_tab_fetch on {}. Solving...", url)
        from hunter.collect.firewall_pow import solve_firewall_pow
        from hunter.collect.lightfetch import V1_FETCH_SCRIPT

        solved = await solve_firewall_pow(page, html=html)
        if solved:
            logger.info("firewallPoW unblock verified! Retrying in-tab fetch on {}", url)
            result = await asyncio.wait_for(
                page.evaluate(V1_FETCH_SCRIPT, url),
                timeout=timeout_sec,
            )
            status = int(result.get("status", 0))
            final_url = str(result.get("url") or url)
            html = str(result.get("text") or "")
            error = result.get("error")
    return status, final_url, html, error


async def handle_fetch_exception(
    session: Any,
    slot: ProfileSlot | None,
    page: Any,
    url: str,
    exc: Exception,
) -> tuple[int, str, str, str | None]:
    logger.warning(
        'evaluate_in_tab_fetch error on {} (profile={}): '
        '{}', url, slot.profile_id if slot else "default", exc)
    try:
        if not page.is_closed():
            await page.close()
    except Exception:
        pass
    if slot is not None:
        slot.light_page = None
        slot.light_nav_count = 0
        if slot.index == 0:
            session._light_page = None
            session._light_nav_count = 0
    else:
        session._light_page = None
        session._light_nav_count = 0

    if is_target_closed_error(exc):
        logger.warning(
            'TargetClosedError in evaluate_in_tab_fetch on {}. Restarting '
            'browser...', url)
        try:
            await session.restart()
        except Exception as r_exc:
            logger.error("Failed restarting browser after TargetClosedError: {}", r_exc)

    return 0, url, "", str(exc)
