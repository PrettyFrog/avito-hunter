"""Cookie and challenge helpers shared across challenge recovery paths."""

from __future__ import annotations

import inspect
from typing import Any


def is_operator_captcha(challenge_type: str, html: str) -> bool:
    hl = html.lower()
    return (
        challenge_type.lower() in ("geetest", "hcaptcha", "smartcaptcha")
        or any(k in hl for k in ("geetest", "hcaptcha", "h-captcha", "smartcaptcha", "js-form-captcha-image"))
    )


async def extract_page_cookies(page: Any) -> list[dict[str, Any]]:
    fn = getattr(page, "cookies", None) or getattr(getattr(page, "context", None), "cookies", None)
    if fn:
        return await fn() if inspect.iscoroutinefunction(fn) else fn()
    return []


def merge_cookies(existing: list[dict[str, Any]], req_cookies: Any) -> list[dict[str, Any]]:
    from hunter.transport.recovery_transport import merge_scoped_cookies
    return merge_scoped_cookies(existing, req_cookies)
