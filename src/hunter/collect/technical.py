from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from hunter.collect.technical_parts.parse_show_technical_01 import extract_technical_fields

__all__ = [
    "SHOW_TECHNICAL_URL",
    "TechnicalSessionInfo",
    "check_technical_session",
    "parse_show_technical",
]

SHOW_TECHNICAL_URL = "https://www.avito.ru/info/show_technical"


@dataclass
class TechnicalSessionInfo:
    ip: str | None = None
    trust_level: str | None = None
    city: str | None = None
    region: str | None = None
    cookies_enabled: bool | None = None
    server: str | None = None
    user_agent: str | None = None
    raw_fields: dict[str, str] = field(default_factory=dict)

    @property
    def is_trusted(self) -> bool:
        """Returns True if trust level is high or normal."""
        if not self.trust_level:
            return True  # Unknown, assume ok
        tl = self.trust_level.strip().lower()
        return "низк" not in tl and "заблокир" not in tl and "low" not in tl


def parse_show_technical(html: str) -> TechnicalSessionInfo:
    """Parses Avito's service diagnostics page `/info/show_technical`.

    Extracts external IP, trust level, geolocation, cookie status, and server node.
    """
    data = extract_technical_fields(html)
    return TechnicalSessionInfo(
        ip=data["ip"],
        trust_level=data["trust_level"],
        city=data["city"],
        region=data["region"],
        cookies_enabled=data["cookies_enabled"],
        server=data["server"],
        user_agent=data["user_agent"],
        raw_fields=data["raw_fields"],
    )


async def check_technical_session(
    browser: Any | None = None,
    *,
    proxy: str | None = None,
    timeout_sec: float = 15.0,
) -> TechnicalSessionInfo:
    """Safely checks Avito session health, IP and trust level via /info/show_technical.

    Uses light in-browser fetch if browser session is available; falls back to httpx.
    """
    html = ""
    if browser is not None:
        try:
            from hunter.collect.lightfetch import fetch_v1
            context = getattr(browser, "context", None)
            if context is not None:
                status, _, page_text, err = await fetch_v1(
                    context,
                    SHOW_TECHNICAL_URL,
                    timeout_sec=timeout_sec,
                    browser=browser,
                )
                if status == 200 and page_text:
                    html = page_text
        except Exception as exc:
            logger.debug("Failed to check /info/show_technical via browser light tab: {}", exc)

    if not html:
        # Fallback via httpx
        try:
            import httpx
            client_kwargs: dict[str, Any] = {
                "timeout": timeout_sec,
                "follow_redirects": True,
                "headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            }
            if proxy:
                client_kwargs["proxy"] = proxy

            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(SHOW_TECHNICAL_URL)
                if resp.status_code == 200:
                    html = resp.text
        except Exception as exc:
            logger.warning("Failed to check /info/show_technical via httpx: {}", exc)

    return parse_show_technical(html)
