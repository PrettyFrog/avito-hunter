from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from hunter.collect.browser_parts.is_target_closed_error_01 import (
    PROFILE_IDENTITIES,
    ProfileSlot,
    _is_challenge_asset,
)


async def _init_single_secondary_profile(
    idx: int,
    new_ctx_fn: Any,
    profiles_dir: Path,
    settings: Any,
    block_assets: bool,
) -> ProfileSlot | None:
    identity = PROFILE_IDENTITIES[idx % len(PROFILE_IDENTITIES)]
    storage_path = profiles_dir / f"profile_{idx}.json"
    ctx_kwargs: dict[str, Any] = {
        "user_agent": identity["user_agent"],
        "viewport": dict(identity["viewport"]),
        "locale": "ru-RU",
        "timezone_id": getattr(settings, "timezone", "Europe/Moscow"),
    }
    if storage_path.exists():
        try:
            json.loads(storage_path.read_text(encoding="utf-8"))
            ctx_kwargs["storage_state"] = str(storage_path)
        except Exception as exc:
            logger.warning("Corrupt storage_state at {}, starting clean: {}", storage_path, exc)

    maybe_ctx = new_ctx_fn(**ctx_kwargs)
    if not asyncio.iscoroutine(maybe_ctx) and not hasattr(maybe_ctx, "__await__"):
        return None
    sec_ctx = await maybe_ctx
    if hasattr(sec_ctx, "add_init_script"):
        await sec_ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
    if block_assets and hasattr(sec_ctx, "route"):
        async def _sec_route_filter(route: Any) -> None:
            try:
                if _is_challenge_asset(route.request.url):
                    await route.continue_()
                elif route.request.resource_type in ["image", "media", "font"]:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        await sec_ctx.route("**/*", _sec_route_filter)
    return ProfileSlot(
        profile_id=f"profile_{idx}",
        index=idx,
        context=sec_ctx,
        user_agent=identity["user_agent"],
        viewport=dict(identity["viewport"]),
        storage_path=storage_path,
    )


async def init_secondary_profiles_db(
    context: Any,
    profiles: list[ProfileSlot],
    profile_count: int,
    settings: Any,
    block_assets: bool,
) -> None:
    """Initialize isolated BrowserContext slots (profile_1 .. profile_{N-1}) inside the same Chromium instance."""
    if profile_count <= 1 or context is None:
        return
    browser_obj = getattr(context, "browser", None)
    if browser_obj is None or not hasattr(browser_obj, "new_context"):
        return
    new_ctx_fn = getattr(browser_obj, "new_context")
    if not callable(new_ctx_fn):
        return

    profiles_dir = getattr(settings, "profiles_dir", None)
    if not isinstance(profiles_dir, Path):
        profiles_dir = Path(settings.profile_dir).parent / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)

    if profiles:
        profiles[0].storage_path = profiles_dir / "profile_0.json"

    for idx in range(1, profile_count):
        try:
            slot = await _init_single_secondary_profile(
                idx, new_ctx_fn, profiles_dir, settings, block_assets
            )
            if slot is None:
                break
            profiles.append(slot)
        except Exception as exc:
            logger.warning("Could not create secondary browser profile_{}: {}", idx, exc)
            break

    if len(profiles) > 1:
        logger.info(
            "Initialized {} isolated browser session profiles in single Chromium instance",
            len(profiles),
        )


def cleanup_profile_locks(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = profile_dir / lock_name
        if lock_path.exists() or lock_path.is_symlink():
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass


def build_browser_launch_config(
    settings: Any,
    profile_count: int,
) -> tuple[bool, dict[str, Any]]:
    import os

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    headless_env = os.environ.get("HUNTER_HEADLESS")
    if headless_env is not None:
        is_headless = headless_env.lower() in ("1", "true", "yes")
    else:
        is_headless = not has_display

    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-default-apps",
        "--mute-audio",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-session-crashed-bubble",
        "--enable-low-end-device-mode",
        "--disk-cache-size=1",
        "--media-cache-size=1",
    ]
    if is_headless:
        browser_args.append("--headless=new")

    if profile_count <= 1:
        browser_args.extend([
            "--single-process",
            "--no-zygote",
            "--js-flags=--max-old-space-size=256",
        ])
    else:
        browser_args.extend([
            "--renderer-process-limit=2",
            "--process-per-site",
            "--disable-site-isolation-trials",
            "--disable-features=IsolateOrigins,site-per-process",
            "--js-flags=--max-old-space-size=256",
        ])

    kwargs = dict(
        headless=is_headless,
        viewport={"width": 1280, "height": 800},
        locale="ru-RU",
        timezone_id=settings.timezone,
        args=browser_args,
        ignore_default_args=["--enable-automation"],
    )
    return is_headless, kwargs


async def launch_persistent_context_db(
    playwright_obj: Any,
    settings: Any,
    kwargs: dict[str, Any],
) -> Any:
    try:
        return await playwright_obj.chromium.launch_persistent_context(
            str(settings.profile_dir),
            channel="chrome",
            **kwargs,
        )
    except Exception as exc:
        logger.warning("Chrome channel unavailable ({}), falling back to bundled Chromium", exc)
        return await playwright_obj.chromium.launch_persistent_context(
            str(settings.profile_dir),
            **kwargs,
        )


async def setup_main_route_filter_if_blocked(context: Any, settings: Any) -> bool:
    import os

    browser_cfg = settings.browser or {}
    block_assets = browser_cfg.get("block_assets", False) or os.environ.get(
        "HUNTER_BLOCK_ASSETS", "0"
    ).lower() in ("1", "true", "yes")

    if block_assets and hasattr(context, "route"):
        async def _main_route_filter(route: Any) -> None:
            try:
                if _is_challenge_asset(route.request.url):
                    await route.continue_()
                elif route.request.resource_type in ["image", "media", "font"]:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        await context.route("**/*", _main_route_filter)
        logger.info("Asset blocking enabled (image, media, font) with captcha bypass")
    return block_assets
