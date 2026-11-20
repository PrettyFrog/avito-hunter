from __future__ import annotations

import re
from typing import Any

from loguru import logger

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        HTMLParser = None  # type: ignore[assignment,misc]


def extract_raw_fields_from_tree(html: str) -> dict[str, str]:
    raw_fields: dict[str, str] = {}
    if not (HTMLParser and html):
        return raw_fields
    try:
        tree = HTMLParser(html)
        # 1. Parse standard table rows (tr > td/th)
        for row in tree.css("tr"):
            cells = row.css("th, td")
            if len(cells) >= 2:
                k = cells[0].text(strip=True).rstrip(":")
                v = cells[1].text(strip=True)
                if k and v:
                    raw_fields[k] = v

        # 2. Parse definition lists (dl > dt, dd) by sibling traversal
        for dt in tree.css("dt"):
            k = dt.text(strip=True).rstrip(":")
            node = dt.next
            while node and node.tag != "dd" and node.tag != "dt":
                node = node.next
            if node and node.tag == "dd":
                v = node.text(strip=True)
                if k and v:
                    raw_fields[k] = v

        # 3. Parse paragraph / div key-values
        for el in tree.css("p, div"):
            txt = el.text(strip=True)
            if ":" in txt and "\n" not in txt and len(txt) < 200:
                parts = txt.split(":", 1)
                k = parts[0].strip()
                v = parts[1].strip()
                if k and v and k not in raw_fields:
                    raw_fields[k] = v
    except Exception as exc:
        logger.debug("HTMLParser error parsing show_technical: {}", exc)
    return raw_fields


def search_field(patterns: list[str], raw_fields: dict[str, str], html: str) -> str | None:
    for p in patterns:
        for k, v in raw_fields.items():
            if re.search(p, k, re.IGNORECASE):
                return v
        m = re.search(rf"(?:{p})\s*[:\-=]\s*([^\r\n<,]+)", html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def parse_cookies_enabled(cookies_str: str | None) -> bool | None:
    if not cookies_str:
        return None
    cookies_lower = cookies_str.lower()
    if any(s in cookies_lower for s in ("включ", "yes", "true", "on")):
        return True
    if any(s in cookies_lower for s in ("выключ", "отключ", "no", "false", "off")):
        return False
    return None


def extract_technical_fields(html: str) -> dict[str, Any]:
    raw_fields = extract_raw_fields_from_tree(html)

    ip = search_field(
        [r"ай-?пи(?:\s*адрес)?", r"ip(?:\s*address)?", r"адрес\s*клиента"], raw_fields, html
    )
    if ip:
        ip_m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]{7,})", ip)
        if ip_m:
            ip = ip_m.group(1)

    trust = search_field([r"уровень\s*доверия", r"доверие", r"trust\s*level"], raw_fields, html)
    city = search_field([r"ваш\s*город", r"город", r"city"], raw_fields, html)
    region = search_field([r"ваш\s*регион", r"регион", r"region"], raw_fields, html)
    server = search_field([r"сервер", r"server", r"нода", r"node"], raw_fields, html)
    cookies_str = search_field([r"куки", r"cookies"], raw_fields, html)
    user_agent = search_field([r"user-?agent", r"браузер", r"клиент"], raw_fields, html)

    return {
        "ip": ip,
        "trust_level": trust,
        "city": city,
        "region": region,
        "cookies_enabled": parse_cookies_enabled(cookies_str),
        "server": server,
        "user_agent": user_agent,
        "raw_fields": raw_fields,
    }
