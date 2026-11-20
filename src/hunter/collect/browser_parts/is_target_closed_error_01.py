from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hunter.core.config import get_settings

try:
    from playwright._impl._errors import TargetClosedError
except ImportError:
    TargetClosedError = None


def is_target_closed_error(exc: BaseException) -> bool:
    if TargetClosedError is not None and isinstance(exc, TargetClosedError):
        return True
    msg = str(exc).lower()
    return (
        "target closed" in msg
        or "browser has been closed" in msg
        or "target page, context or browser has been closed" in msg
        or "session closed" in msg
    )


def is_context_closed(ctx: Any) -> bool:
    if ctx is None:
        return True
    try:
        if hasattr(ctx, "is_closed") and callable(ctx.is_closed):
            val = ctx.is_closed()
            if isinstance(val, bool) and val:
                return True
        browser = getattr(ctx, "browser", None)
        if browser is not None and hasattr(browser, "is_connected") and callable(browser.is_connected):
            if not browser.is_connected():
                return True
        return False
    except Exception:
        return True


PROFILE_IDENTITIES: list[dict[str, Any]] = [
    {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1280, "height": 800},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1366, "height": 768},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1440, "height": 900},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1536, "height": 864},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1600, "height": 900},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1920, "height": 1080},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1680, "height": 1050},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1360, "height": 768},
    },
]


def _get_configured_profile_count(settings: Any) -> int:
    collect_cfg = getattr(settings, "collect", None)
    raw = getattr(collect_cfg, "profile_count", 1) if collect_cfg is not None else 1
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(1, min(raw, 16))
    return 1


def _get_configured_cooldown_minutes(settings: Any) -> float:
    collect_cfg = getattr(settings, "collect", None)
    raw = getattr(collect_cfg, "profile_cooldown_minutes", 15) if collect_cfg is not None else 15
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(0.1, float(raw))
    return 15.0


@dataclass
class ProfileSlot:
    profile_id: str
    index: int
    context: Any
    user_agent: str = ""
    viewport: dict[str, int] = field(default_factory=lambda: {"width": 1280, "height": 800})
    storage_path: Path | None = None
    light_page: Any = None
    light_nav_count: int = 0
    light_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cooldown_until: float = 0.0
    consecutive_errors: int = 0
    total_requests: int = 0
    last_request_ts: float = 0.0

    def is_healthy(self, now_mono: float | None = None) -> bool:
        now_val = now_mono if now_mono is not None else time.monotonic()
        return now_val >= self.cooldown_until


def _is_challenge_asset(url: str) -> bool:
    url_l = (url or "").lower()
    return any(k in url_l for k in ("captcha", "geetest", "firewall", "smartcaptcha", "verify", "security"))


def _blank_physical_screen() -> None:
    """Power off physical display panels if running on headless server with Xvfb."""
    try:
        import fcntl
        for dev in ("/dev/fb0", "/dev/fb1"):
            try:
                with open(dev, "wb") as f:
                    fcntl.ioctl(f, 0x4611, 4)  # FBIOBLANK, FB_BLANK_POWERDOWN
            except Exception:
                pass
    except Exception:
        pass


def is_browser_session_locked(profile_dir: Any = None) -> tuple[bool, str]:
    """Checks whether the browser profile is locked or the systemd service is active."""
    import subprocess

    # 1. Check systemd service status
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "avito-hunter.service"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip() == "active":
            return True, "сервис avito-hunter активен"
    except Exception:
        pass

    # 2. Check Chromium lock files
    p_dir = Path(profile_dir) if profile_dir else get_settings().profile_dir
    if p_dir.exists():
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = p_dir / lock_name
            if lock_path.exists() or lock_path.is_symlink():
                return True, f"профиль Chromium заблокирован ({lock_name})"

    return False, ""


def init_browser_session(session: Any) -> None:
    session._playwright = None
    session.context = None
    session._page = None
    session._nav_count = 0
    session._consecutive_timeouts = 0
    session._light_page = None
    session._light_nav_count = 0
    session._light_lock = asyncio.Lock()
    session.profiles = []
