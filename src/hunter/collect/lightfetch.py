from __future__ import annotations

from typing import Any

from loguru import logger

from hunter.collect.lightfetch_parts.check_block_markers_05 import (
    calculate_field_coverage,
    check_block_markers,
    check_field_coverage_drop,
    compare_hits,
    parse_shadow_search,
)
from hunter.collect.lightfetch_parts.v1_fetch_script_01 import (
    V1_FETCH_SCRIPT,
    evaluate_v1_page_fetch,
    evaluate_v2_page_fetch,
    try_browser_tab_fetch,
)

__all__ = [
    "V1_FETCH_SCRIPT",
    "calculate_field_coverage",
    "check_block_markers",
    "check_field_coverage_drop",
    "compare_hits",
    "fetch_v1",
    "fetch_v2",
    "parse_shadow_search",
]


async def fetch_v1(
    context: Any,
    url: str,
    timeout_sec: float = 15.0,
    browser: Any | None = None,
    profile_id: str | None = None,
) -> tuple[int, str, str, str | None]:
    """Variant 1: evaluate in-browser fetch() from a lightweight same-origin tab (robots.txt).
    
    If browser session with persistent light tab is provided, reuses that tab.
    Returns: (status, final_url, html, error)
    """
    tab_res = await try_browser_tab_fetch(browser, url, timeout_sec, profile_id)
    if tab_res is not None:
        return tab_res

    target_ctx = context
    if browser is not None and profile_id is not None and hasattr(browser, "get_profile"):
        try:
            slot = browser.get_profile(profile_id)
            if slot is not None and getattr(slot, "context", None) is not None:
                target_ctx = slot.context
        except Exception:
            pass

    try:
        page = await target_ctx.new_page()
    except Exception as exc:
        from hunter.collect.browser import is_target_closed_error
        if browser is not None and is_target_closed_error(exc):
            logger.warning("TargetClosedError creating page in fetch_v1. Restarting browser...")
            await browser.restart()
            target_ctx = browser.context
            if profile_id is not None and hasattr(browser, "get_profile"):
                slot = browser.get_profile(profile_id)
                if slot is not None and getattr(slot, "context", None) is not None:
                    target_ctx = slot.context
            page = await target_ctx.new_page()
        else:
            raise
    try:
        return await evaluate_v1_page_fetch(page, url, timeout_sec)
    except Exception as exc:
        logger.warning("fetch_v1 error on {}: {}", url, exc)
        return 0, url, "", str(exc)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def fetch_v2(
    context: Any,
    url: str,
    timeout_sec: float = 15.0,
    browser: Any | None = None,
    profile_id: str | None = None,
) -> tuple[int, str, str, str | None]:
    """Variant 2: top-level navigation in a separate tab with page.route aborting non-document resources.
    
    Uses standard Sec-Fetch-Dest: document headers.
    Returns: (status, final_url, html, error)
    """
    target_ctx = context
    if browser is not None and profile_id is not None and hasattr(browser, "get_profile"):
        try:
            slot = browser.get_profile(profile_id)
            if slot is not None and getattr(slot, "context", None) is not None:
                target_ctx = slot.context
        except Exception:
            pass

    try:
        page = await target_ctx.new_page()
    except Exception as exc:
        from hunter.collect.browser import is_target_closed_error
        if browser is not None and is_target_closed_error(exc):
            logger.warning("TargetClosedError creating page in fetch_v2. Restarting browser...")
            await browser.restart()
            target_ctx = browser.context
            if profile_id is not None and hasattr(browser, "get_profile"):
                slot = browser.get_profile(profile_id)
                if slot is not None and getattr(slot, "context", None) is not None:
                    target_ctx = slot.context
            page = await target_ctx.new_page()
        else:
            raise
    try:
        return await evaluate_v2_page_fetch(page, url, timeout_sec)
    except Exception as exc:
        logger.warning("fetch_v2 error on {}: {}", url, exc)
        cur_url = url
        try:
            cur_url = page.url
        except Exception:
            pass
        return 0, cur_url, "", str(exc)
    finally:
        try:
            await page.close()
        except Exception:
            pass
