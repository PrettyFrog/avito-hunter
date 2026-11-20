from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from playwright.async_api import async_playwright

from hunter.collect.browser_parts.browsersession__ensure_default_profile_03 import (
    acquire_profile_db,
    ensure_default_profile_db,
    force_gc_db,
    get_or_create_page_db,
    get_profile_db,
    get_profiles_status_db,
    mark_profile_blocked_db,
    mark_profile_ok_db,
    safe_recycle_tab_db,
    save_profile_states_db,
)
from hunter.collect.browser_parts.browsersession_fetch_14 import (
    fetch_page_db,
)
from hunter.collect.browser_parts.browsersession_get_or_create_light_page_06 import (
    get_or_create_light_page_db,
    handle_fetch_exception,
    handle_pow_challenge_if_needed,
    record_fetch_stats_and_gc,
    recycle_light_page_db,
)
from hunter.collect.browser_parts.browsersession__init_secondary_profiles_08 import (
    build_browser_launch_config,
    cleanup_profile_locks,
    init_secondary_profiles_db,
    launch_persistent_context_db,
    setup_main_route_filter_if_blocked,
)
from hunter.collect.browser_parts.browsersession_start_11 import (
    close_browser_session_db,
    init_session_page,
    prepare_fetch_page,
    restart_browser_session_db,
)
from hunter.collect.browser_parts.is_target_closed_error_01 import (
    PROFILE_IDENTITIES,
    ProfileSlot,
    _blank_physical_screen,
    _get_configured_cooldown_minutes,
    _get_configured_profile_count,
    _is_challenge_asset,
    init_browser_session,
    is_browser_session_locked,
    is_context_closed,
    is_target_closed_error,
)
from hunter.core.config import get_settings
from hunter.core.models import FetchResult

__all__ = [
    "BrowserSession",
    "ProfileSlot",
    "is_browser_session_locked",
    "is_target_closed_error",
    "_is_challenge_asset",
]


class BrowserSession:
    def __init__(self) -> None:
        init_browser_session(self)

    def _ensure_default_profile(self) -> list[ProfileSlot]:
        return ensure_default_profile_db(self)

    def get_profile(self, profile_id: str | None = None) -> ProfileSlot | None:
        return get_profile_db(self, profile_id)

    def acquire_profile(self, exclude_ids: set[str] | None = None) -> ProfileSlot:
        (
            'Select the best healthy profile using '
            'Least-Recently-Used (LRU) and lock-free '
            'preference.'
        )
        return acquire_profile_db(self, exclude_ids)

    async def mark_profile_blocked(
        self,
        profile_id: str,
        reason: str = "blocked",
        cooldown_minutes: float | None = None,
    ) -> bool:
        (
            'Put a specific profile on cooldown, reset its '
            'light tab, and return True if ALL profiles are now'
            ' blocked.'
        )
        return await mark_profile_blocked_db(self, profile_id, reason, cooldown_minutes)

    def mark_profile_ok(self, profile_id: str) -> None:
        """Mark a profile as healthy after a clean search response."""
        mark_profile_ok_db(self, profile_id)

    def get_profiles_status(self) -> list[dict[str, Any]]:
        """Return diagnostic status of all session profiles."""
        return get_profiles_status_db(self)

    async def save_profile_states(self) -> None:
        """Persist storage_state (cookies + localStorage) for all profiles to disk."""
        await save_profile_states_db(self)

    async def _force_gc(self, page: Any = None) -> None:
        """Trigger Chromium V8 HeapProfiler garbage collection via CDP."""
        await force_gc_db(self, page)

    async def _get_or_create_page(self) -> Any:
        return await get_or_create_page_db(self)

    async def _safe_recycle_tab(self) -> Any:
        """Safely rotate tab: open new page before closing old one."""
        return await safe_recycle_tab_db(self)

    async def get_or_create_light_page(self, profile_id: str | None = None) -> Any:
        """Retrieve persistent same-origin tab for light in-tab fetch, recycling if necessary."""
        return await get_or_create_light_page_db(self, profile_id)

    async def _recycle_light_page(self, profile_id: str | None = None) -> Any:
        """Safely rotate light fetch tab: create and navigate new tab before closing old one."""
        return await recycle_light_page_db(self, profile_id)

    async def evaluate_in_tab_fetch(
        self,
        url: str,
        timeout_sec: float = 15.0,
        profile_id: str | None = None,
    ) -> tuple[int, str, str, str | None]:
        """Execute in-tab fetch() using persistent same-origin tab of the selected profile."""
        slot = self.get_profile(profile_id)
        lock = slot.light_lock if slot is not None else self._light_lock
        async with lock:
            page = await self.get_or_create_light_page(profile_id=slot.profile_id if slot else None)
            from hunter.collect.lightfetch import V1_FETCH_SCRIPT

            try:
                result = await asyncio.wait_for(
                    page.evaluate(V1_FETCH_SCRIPT, url),
                    timeout=timeout_sec,
                )
                await record_fetch_stats_and_gc(self, slot, page)
                status = int(result.get("status", 0))
                final_url = str(result.get("url") or url)
                html = str(result.get("text") or "")
                error = result.get("error")

                return await handle_pow_challenge_if_needed(
                    page, url, status, final_url, html, error, timeout_sec
                )
            except Exception as exc:
                return await handle_fetch_exception(self, slot, page, url, exc)

    async def _init_secondary_profiles(
        self,
        profile_count: int,
        settings: Any,
        block_assets: bool,
    ) -> None:
        (
            'Initialize isolated BrowserContext slots '
            '(profile_1 .. profile_{N-1}) inside the same '
            'Chromium instance.'
        )
        await init_secondary_profiles_db(
            self.context,
            self.profiles,
            profile_count,
            settings,
            block_assets,
        )

    async def start(self) -> None:
        settings = get_settings()
        cleanup_profile_locks(settings.profile_dir)
        profile_count = _get_configured_profile_count(settings)

        self._playwright = await async_playwright().start()

        is_headless, kwargs = build_browser_launch_config(settings, profile_count)
        self.context = await launch_persistent_context_db(self._playwright, settings, kwargs)
        _blank_physical_screen()
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        block_assets = await setup_main_route_filter_if_blocked(self.context, settings)

        self.profiles = [
            ProfileSlot(
                profile_id="profile_0",
                index=0,
                context=self.context,
                user_agent=PROFILE_IDENTITIES[0]["user_agent"],
                viewport=dict(PROFILE_IDENTITIES[0]["viewport"]),
                light_lock=self._light_lock,
            )
        ]
        await self._init_secondary_profiles(profile_count, settings, block_assets)

        logger.info(
            "browser profile at {} (headless={}, block_assets={}, profiles={})",
            settings.profile_dir,
            is_headless,
            block_assets,
            len(self.profiles),
        )
        self._page, self._nav_count = await init_session_page(self.context)

        await self.save_profile_states()

    async def close(self) -> None:
        await close_browser_session_db(self)

    async def restart(self) -> None:
        """Cleanly restart browser context and playwright driver to free process memory."""
        await restart_browser_session_db(self)

    async def fetch(self, url: str, _is_retry: bool = False) -> FetchResult:
        if is_context_closed(self.context):
            await self.restart()
        assert self.context is not None

        try:
            page = await prepare_fetch_page(self)
        except Exception as exc:
            if not _is_retry and is_target_closed_error(exc):
                logger.warning(
                    'TargetClosedError in fetch tab setup on {}. '
                    'Restarting browser...',
                    url,
                )
                await self.restart()
                return await self.fetch(url, _is_retry=True)
            raise

        return await fetch_page_db(self, page, url, _is_retry=_is_retry)
