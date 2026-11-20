"""Typed captcha provider dispatcher resolution and fallback bounding (T-977)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import re
from typing import Any

import httpx
from hunter.transport.metered_http import HttpDeferred

logger = logging.getLogger(__name__)
AVITO_GEETEST_CAPTCHA_ID = "2d9c743cf7d63dbc9db578a608196bcd"


class CaptchaProviderType(str, Enum):
    GEETEST = "geetest"
    HCAPTCHA = "hcaptcha"
    INTERNAL = "internal"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CaptchaDispatchInfo:
    provider_type: CaptchaProviderType
    captcha_id: str | None = None
    is_supported: bool = False
    details: str = ""


def parse_captcha_dispatch_payload(captcha_dict: dict[str, Any]) -> CaptchaDispatchInfo:
    """Classifies typed provider from firewall captcha dictionary."""
    if not isinstance(captcha_dict, dict) or not captcha_dict:
        return CaptchaDispatchInfo(CaptchaProviderType.UNKNOWN, details="Empty dispatcher dict")
    if captcha_dict.get("disabled") or captcha_dict.get("isDisabled"):
        return CaptchaDispatchInfo(CaptchaProviderType.DISABLED, details="Captcha disabled by server")
    if "geeTest" in captcha_dict or "geetest" in captcha_dict:
        gt = captcha_dict.get("geeTest") or captcha_dict.get("geetest") or {}
        cid = gt.get("captcha_id") or gt.get("captchaId") or captcha_dict.get("captcha_id") or captcha_dict.get("captchaId")
        return CaptchaDispatchInfo(CaptchaProviderType.GEETEST, captcha_id=cid or AVITO_GEETEST_CAPTCHA_ID, is_supported=True)
    if any(k in captcha_dict for k in ("hCaptcha", "hcaptcha", "h_captcha")):
        return CaptchaDispatchInfo(CaptchaProviderType.HCAPTCHA, details="hCaptcha required (unsupported)")
    if any(k in captcha_dict for k in ("internal", "smartCaptcha", "smart_captcha", "internalCaptcha")):
        return CaptchaDispatchInfo(CaptchaProviderType.INTERNAL, details="Internal/smart captcha (unsupported)")
    if cid := (captcha_dict.get("captcha_id") or captcha_dict.get("captchaId")):
        return CaptchaDispatchInfo(CaptchaProviderType.GEETEST, captcha_id=cid, is_supported=True)
    return CaptchaDispatchInfo(CaptchaProviderType.UNKNOWN, details=f"Unknown captcha keys: {list(captcha_dict.keys())}")


async def resolve_firewall_captcha_dispatch(
    client: httpx.AsyncClient, html: str = ""
) -> CaptchaDispatchInfo:
    """Resolves captcha provider via HTML inspection or Avito dispatcher API."""
    m = re.search(r"""(?:captchaId|captcha_id)["']?\s*[:=]\s*["']([0-9a-f]{32})["']""", html, re.I)
    if m:
        return CaptchaDispatchInfo(CaptchaProviderType.GEETEST, captcha_id=m.group(1), is_supported=True)
    try:
        r = await client.post(
            "https://www.avito.ru/web/5/firewallCaptcha/get", json={"refreshInternalCaptcha": False},
            headers={"Referer": "https://www.avito.ru/", "X-Source": "client-browser", "Accept": "application/json"},
        )
        if r.status_code == 200:
            data = r.json()
            captcha_data = data.get("success", {}).get("result", {}).get("captcha", {})
            return parse_captcha_dispatch_payload(captcha_data)
    except HttpDeferred:
        raise
    except Exception as exc:
        logger.warning("Failed to query firewallCaptcha/get: %s", type(exc).__name__)
    return CaptchaDispatchInfo(CaptchaProviderType.UNKNOWN, details="Failed to query dispatcher API")
