"""Search light fetch helper functions (part 1)."""

from __future__ import annotations

from typing import Any

from hunter.core.models import FetchResult


async def execute_light_variant(
    browser: Any,
    url: str,
    variant: str,
    profile_id: str | None,
) -> tuple[int, str, str, str | None]:
    from hunter.collect.lightfetch import fetch_v1, fetch_v2

    if variant == "fetch":
        if profile_id is not None:
            try:
                return await fetch_v1(
                    browser.context,
                    url,
                    browser=browser,
                    profile_id=profile_id,
                )
            except TypeError:
                return await fetch_v1(browser.context, url, browser=browser)
        return await fetch_v1(browser.context, url, browser=browser)

    if profile_id is not None:
        try:
            return await fetch_v2(
                browser.context,
                url,
                browser=browser,
                profile_id=profile_id,
            )
        except TypeError:
            return await fetch_v2(browser.context, url)
    return await fetch_v2(browser.context, url)


def build_light_fetch_result(
    url: str,
    status: int,
    html: str,
    final_url: str,
    lat_ms: int,
    err: str | None,
    profile_id: str | None,
) -> FetchResult:
    from hunter.collect.captcha_pause import looks_like_captcha

    captcha = looks_like_captcha(html, final_url)
    blocked = status in {401, 403, 429} or captcha

    return FetchResult(
        url=url,
        status=status,
        html=html,
        final_url=final_url,
        captcha=captcha,
        blocked=blocked,
        latency_ms=lat_ms,
        timeout=bool(err and "timeout" in err.lower()),
        profile_id=profile_id or "profile_0",
    )
