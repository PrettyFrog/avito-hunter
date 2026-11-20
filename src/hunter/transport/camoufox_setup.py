"""Camoufox launch options tuned for Avito (RU identity, no runtime download)."""

from __future__ import annotations

import sys
from typing import Any


def _installed_build() -> str | None:
    """Falls back to an installed build when the pinned one is absent.

    The library pins a browser release; launching it would download from GitHub
    on first use. Production may not have that access, so prefer what is here.
    """
    try:
        from camoufox.browser_pin import effective_pin
        from camoufox.multiversion import find_installed_version, list_installed, load_config
    except Exception:
        return None
    try:
        pin = effective_pin(load_config())
        if pin is None or find_installed_version(pin.spec) is not None:
            return None
        installed = list_installed()
        return installed[0].version.build if installed else None
    except Exception:
        return None


def camoufox_options(headless: Any, proxy: dict[str, str] | None) -> dict[str, Any]:
    """Target OS fingerprint with the RU locale/timezone measured to pass Qrator."""
    target_os = "linux" if sys.platform.startswith("linux") else "windows"
    options: dict[str, Any] = {
        "headless": headless,
        "os": target_os,
        "locale": "ru-RU",
        "humanize": True,
        "geoip": False,
        "proxy": proxy,
        "firefox_user_prefs": {"dom.ipc.processCount": 2},
        "i_know_what_im_doing": True,
    }
    build = _installed_build()
    if build is not None:
        options["browser"] = build
    return options
