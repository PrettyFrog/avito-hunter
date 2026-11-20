from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

V1_FETCH_SCRIPT = """
async (targetUrl) => {
    try {
        const resp = await fetch(targetUrl, {
            credentials: 'include',
            referrer: "https://www.avito.ru/",
            headers: {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            }
        });
        const text = await resp.text();
        return {
            status: resp.status,
            url: resp.url,
            text: text,
            error: null
        };
    } catch (err) {
        return {
            status: 0,
            url: targetUrl,
            text: "",
            error: String(err)
        };
    }
}
"""


async def try_browser_tab_fetch(
    browser: Any | None,
    url: str,
    timeout_sec: float,
    profile_id: str | None,
) -> tuple[int, str, str, str | None] | None:
    if browser is None or not hasattr(browser, "evaluate_in_tab_fetch"):
        return None
    func = getattr(browser, "evaluate_in_tab_fetch")
    if not callable(func):
        return None
    try:
        if profile_id is not None:
            try:
                res = func(url, timeout_sec=timeout_sec, profile_id=profile_id)
            except TypeError:
                res = func(url, timeout_sec=timeout_sec)
        else:
            res = func(url, timeout_sec=timeout_sec)
        if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
            return await res
        if isinstance(res, tuple):
            return res
    except Exception as exc:
        logger.debug("browser.evaluate_in_tab_fetch failed: {}", exc)
    return None


async def evaluate_v1_page_fetch(
    page: Any,
    url: str,
    timeout_sec: float,
) -> tuple[int, str, str, str | None]:
    # Navigate to same-origin light page to establish origin & cookie context
    await page.goto("https://www.avito.ru/robots.txt", wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))
    result = await asyncio.wait_for(
        page.evaluate(V1_FETCH_SCRIPT, url),
        timeout=timeout_sec,
    )
    status = int(result.get("status", 0))
    final_url = str(result.get("url") or url)
    html = str(result.get("text") or "")
    error = result.get("error")

    if status == 439 or "firewallPow" in html or "/firewall" in final_url.lower():
        logger.info("HTTP 439 / firewall challenge in fetch_v1 on {}. Solving firewallPoW...", url)
        from hunter.collect.firewall_pow import solve_firewall_pow
        solved = await solve_firewall_pow(page, html=html)
        if solved:
            logger.info("firewallPoW unblock verified! Retrying fetch_v1 on {}", url)
            result = await asyncio.wait_for(
                page.evaluate(V1_FETCH_SCRIPT, url),
                timeout=timeout_sec,
            )
            status = int(result.get("status", 0))
            final_url = str(result.get("url") or url)
            html = str(result.get("text") or "")
            error = result.get("error")

    return status, final_url, html, error


async def evaluate_v2_page_fetch(
    page: Any,
    url: str,
    timeout_sec: float,
) -> tuple[int, str, str, str | None]:
    async def route_handler(route: Any, request: Any) -> None:
        if request.resource_type == "document" or "firewall" in request.url.lower():
            await route.continue_()
        else:
            await route.abort()

    await page.route("**/*", route_handler)
    res = await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))
    status = res.status if res else 0
    final_url = page.url
    html = await page.content()

    if status == 439 or "firewallPow" in html or "/firewall" in final_url.lower():
        logger.info("HTTP 439 / firewall challenge in fetch_v2 on {}. Solving firewallPoW...", url)
        from hunter.collect.firewall_pow import solve_firewall_pow
        solved = await solve_firewall_pow(page, html=html)
        if solved:
            logger.info("firewallPoW unblock verified! Retrying page.goto on {}", url)
            res = await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_sec * 1000))
            status = res.status if res else 0
            final_url = page.url
            html = await page.content()

    return status, final_url, html, None
