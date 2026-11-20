"""Unchanged per-client default request headers shared by slot construction."""

import re
from typing import Any


class HeaderDict(dict):
    """Case-insensitive dictionary for browser headers preserving wire casing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lower_map = {k.lower(): k for k in self}

    def __getitem__(self, key: str) -> str:
        real_key = self._lower_map.get(key.lower(), key)
        return super().__getitem__(real_key)

    def __setitem__(self, key: str, value: str) -> None:
        low = key.lower()
        if low in self._lower_map and self._lower_map[low] != key:
            super().pop(self._lower_map[low], None)
        self._lower_map[low] = key
        super().__setitem__(key, value)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key.lower() in self._lower_map
        return super().__contains__(key)

    def get(self, key: str, default: Any = None) -> Any:
        real_key = self._lower_map.get(key.lower())
        return super().get(real_key, default) if real_key is not None else default

    def pop(self, key: str, default: Any = None) -> Any:
        real_key = self._lower_map.pop(key.lower(), None)
        if real_key is not None:
            return super().pop(real_key, default)
        return super().pop(key, default)


def _extract_chrome_major(user_agent: str) -> str:
    m = re.search(r"Chrome/(\d+)", user_agent)
    return m.group(1) if m else "131"


def _default_browser_headers(user_agent: str) -> HeaderDict:
    ref = "https://www.avito.ru/"
    if "Firefox" in user_agent:
        return HeaderDict({
            "User-Agent": user_agent,
            "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Referer": ref,
        })
    chrome_ver = _extract_chrome_major(user_agent)
    return HeaderDict({
        "User-Agent": user_agent,
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Sec-Ch-Ua": f'"Google Chrome";v="{chrome_ver}", "Chromium";v="{chrome_ver}", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Linux"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": ref,
    })
