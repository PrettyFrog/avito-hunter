from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger
from playwright.async_api import TimeoutError as PlaywrightTimeout

from hunter.collect.captcha_pause import looks_like_captcha
from hunter.collect.browser_parts.browsersession_start_11 import (
    execute_page_fetch_navigation,
)
from hunter.collect.browser_parts.is_target_closed_error_01 import (
    is_target_closed_error,
)
from hunter.core.models import FetchResult


def setup_response_interceptor(page: Any) -> tuple[list, Any]:
    blobs: list = []

    async def on_response(response: Any) -> None:
        try:
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype:
                return
            href = response.url
            if "avito" not in href:
                return
            if response.status != 200:
                return
            data = await response.json()
            if isinstance(data, (dict, list)):
                if isinstance(data, dict):
                    data["_response_url"] = href
                blobs.append(data)
                logger.debug("on_response JSON intercepted: {}", href)
        except Exception:
            return

    page.on("response", on_response)
    return blobs, on_response


async def handle_fetch_timeout(
    session: Any,
    page: Any,
    url: str,
    exc: Exception,
    status: int,
) -> tuple[int, str, str]:
    session._consecutive_timeouts += 1
    logger.warning(
        "Fetch timeout on {} (consecutive={}): {}",
        url,
        session._consecutive_timeouts,
        exc,
    )
    try:
        html = await page.content()
        final_url = page.url
    except Exception:
        html = ""
        final_url = url
    status = status or 0

    await session._safe_recycle_tab()

    if session._consecutive_timeouts >= 2:
        logger.warning(
            "2 consecutive browser timeouts reached. Performing full browser restart..."
        )
        try:
            await session.restart()
        except Exception as restart_exc:
            logger.error("Error restarting browser after timeouts: {}", restart_exc)
        session._consecutive_timeouts = 0
    return status, html, final_url


def build_fetch_result(
    url: str,
    status: int,
    html: str,
    blobs: list,
    final_url: str,
    start_mono: float,
    is_timeout: bool,
) -> FetchResult:
    latency_ms = int((time.monotonic() - start_mono) * 1000)
    captcha = looks_like_captcha(html, final_url)
    blocked = status in {401, 403} or captcha
    return FetchResult(
        url=url,
        status=status,
        html=html,
        json_blobs=blobs,
        final_url=final_url,
        captcha=captcha,
        blocked=blocked,
        latency_ms=latency_ms,
        timeout=is_timeout,
        profile_id="profile_0",
    )


async def fetch_page_db(
    session: Any,
    page: Any,
    url: str,
    _is_retry: bool = False,
) -> FetchResult:
    blobs, on_response = setup_response_interceptor(page)
    start_mono = time.monotonic()
    is_timeout = False
    status = 0
    html = ""
    final_url = url

    try:
        status, html, final_url = await asyncio.wait_for(
            execute_page_fetch_navigation(page, url), timeout=60.0
        )
        session._consecutive_timeouts = 0
    except (PlaywrightTimeout, asyncio.TimeoutError) as exc:
        is_timeout = True
        status, html, final_url = await handle_fetch_timeout(session, page, url, exc, status)
    except Exception as exc:
        if not _is_retry and is_target_closed_error(exc):
            logger.warning(
                'TargetClosedError during _do_fetch on {}. Restarting browser and '
                'retrying...', url)
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            await session.restart()
            return await session.fetch(url, _is_retry=True)
        raise
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    return build_fetch_result(
        url, status, html, blobs, final_url, start_mono, is_timeout
    )
