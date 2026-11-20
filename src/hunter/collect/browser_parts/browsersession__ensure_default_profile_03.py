from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from hunter.core.config import get_settings
from hunter.collect.browser_parts.is_target_closed_error_01 import (
    PROFILE_IDENTITIES,
    ProfileSlot,
    _get_configured_cooldown_minutes,
    is_context_closed,
    is_target_closed_error,
)


def ensure_default_profile_db(session: Any) -> list[ProfileSlot]:
    """Ensure at least profile_0 exists if session.context was injected directly (e.g. in unit tests)."""
    if not session.profiles and session.context is not None:
        identity = PROFILE_IDENTITIES[0]
        slot = ProfileSlot(
            profile_id="profile_0",
            index=0,
            context=session.context,
            user_agent=identity["user_agent"],
            viewport=dict(identity["viewport"]),
            light_page=session._light_page,
            light_nav_count=session._light_nav_count,
            light_lock=session._light_lock,
        )
        session.profiles.append(slot)
    elif session.profiles:
        p0 = session.profiles[0]
        if session.context is not None and p0.context is not session.context:
            p0.context = session.context
        if session._light_page is not None and p0.light_page is None:
            p0.light_page = session._light_page
            p0.light_nav_count = session._light_nav_count
    return session.profiles


def get_profile_db(session: Any, profile_id: str | None = None) -> ProfileSlot | None:
    profiles = session._ensure_default_profile()
    if not profiles:
        return None
    if not profile_id:
        return profiles[0]
    for slot in profiles:
        if slot.profile_id == profile_id:
            return slot
    return profiles[0]


def acquire_profile_db(session: Any, exclude_ids: set[str] | None = None) -> ProfileSlot:
    """Select the best healthy profile using Least-Recently-Used (LRU) and lock-free preference."""
    profiles = session._ensure_default_profile()
    if not profiles:
        identity = PROFILE_IDENTITIES[0]
        dummy = ProfileSlot(
            profile_id="profile_0",
            index=0,
            context=session.context,
            user_agent=identity["user_agent"],
            viewport=dict(identity["viewport"]),
            light_lock=session._light_lock,
        )
        session.profiles.append(dummy)
        profiles = session.profiles

    now_mono = time.monotonic()
    excluded = exclude_ids or set()
    candidates = [p for p in profiles if p.profile_id not in excluded]
    if not candidates:
        candidates = list(profiles)

    healthy = [p for p in candidates if p.is_healthy(now_mono)]
    if healthy:
        chosen = min(
            healthy,
            key=lambda p: (1 if p.light_lock.locked() else 0, p.last_request_ts, p.index),
        )
    else:
        chosen = min(candidates, key=lambda p: (p.cooldown_until, p.index))

    chosen.last_request_ts = now_mono
    return chosen


async def _reset_profile_light_tab(session: Any, target: ProfileSlot) -> None:
    if target.light_page is not None:
        try:
            if not target.light_page.is_closed():
                await target.light_page.close()
        except Exception:
            pass
        target.light_page = None
        target.light_nav_count = 0
        if target.index == 0:
            session._light_page = None
            session._light_nav_count = 0


async def mark_profile_blocked_db(
    session: Any,
    profile_id: str,
    reason: str = "blocked",
    cooldown_minutes: float | None = None,
) -> bool:
    """Put a specific profile on cooldown, reset its light tab, and return True if ALL profiles are now blocked."""
    profiles = session._ensure_default_profile()
    now_mono = time.monotonic()
    if cooldown_minutes is None:
        try:
            cooldown_minutes = _get_configured_cooldown_minutes(get_settings())
        except Exception:
            cooldown_minutes = 15.0
    cooldown_sec = max(1.0, float(cooldown_minutes) * 60.0)

    target = session.get_profile(profile_id)
    if target is not None:
        target.consecutive_errors += 1
        target.cooldown_until = now_mono + cooldown_sec
        target.block_reason = reason
        await _reset_profile_light_tab(session, target)

    healthy_remaining = sum(1 for p in profiles if p.is_healthy(now_mono))
    total_profiles = len(profiles)
    logger.warning(
        "Profile {} placed on cooldown for {:.1f}m due to {} (healthy profiles: {}/{})",
        profile_id,
        cooldown_sec / 60.0,
        reason,
        healthy_remaining,
        total_profiles,
    )
    return healthy_remaining == 0


def mark_profile_ok_db(session: Any, profile_id: str) -> None:
    """Mark a profile as healthy after a clean search response."""
    target = session.get_profile(profile_id)
    if target is not None:
        target.consecutive_errors = 0
        target.cooldown_until = 0.0
        target.block_reason = None


def get_profiles_status_db(session: Any) -> list[dict[str, Any]]:
    """Return diagnostic status of all session profiles."""
    profiles = session._ensure_default_profile()
    now_mono = time.monotonic()
    out: list[dict[str, Any]] = []
    for p in profiles:
        rem = max(0.0, p.cooldown_until - now_mono)
        out.append(
            {
                "profile_id": p.profile_id,
                "index": p.index,
                "healthy": rem <= 0.0,
                "cooldown_remaining_sec": round(rem, 1),
                "block_reason": getattr(p, "block_reason", None),
                "consecutive_errors": p.consecutive_errors,
                "total_requests": p.total_requests,
                "light_nav_count": p.light_nav_count,
            }
        )
    return out


async def save_profile_states_db(session: Any) -> None:
    """Persist storage_state (cookies + localStorage) for all profiles to disk."""
    for slot in session.profiles:
        if not slot.storage_path or slot.context is None:
            continue
        if not hasattr(slot.context, "storage_state"):
            continue
        try:
            slot.storage_path.parent.mkdir(parents=True, exist_ok=True)
            res = slot.context.storage_state(path=str(slot.storage_path))
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            logger.debug("Could not save storage_state for {}: {}", slot.profile_id, exc)


async def force_gc_db(session: Any, page: Any = None) -> None:
    """Trigger Chromium V8 HeapProfiler garbage collection via CDP."""
    try:
        target_page = page or session._light_page or session._page
        if target_page is None and session.context and getattr(session.context, "pages", None):
            target_page = session.context.pages[0]
        if target_page is not None and hasattr(session.context, "new_cdp_session"):
            cdp = await session.context.new_cdp_session(target_page)
            await cdp.send("HeapProfiler.collectGarbage")
            await cdp.detach()
            logger.debug("Chromium V8 garbage collection triggered via CDP")
    except Exception as exc:
        logger.debug("CDP garbage collection skipped: {}", exc)


async def get_or_create_page_db(session: Any) -> Any:
    if is_context_closed(session.context):
        logger.warning("Browser context closed in _get_or_create_page. Restarting browser...")
        await session.restart()
    assert session.context is not None
    if session._page is not None:
        try:
            if not session._page.is_closed():
                return session._page
        except Exception:
            pass
    try:
        if session.context.pages:
            session._page = session.context.pages[0]
        else:
            session._page = await session.context.new_page()
    except Exception as exc:
        if is_target_closed_error(exc):
            logger.warning("TargetClosedError in _get_or_create_page. Restarting browser...")
            await session.restart()
            session._page = session.context.pages[0] if (session.context and session.context.pages) else await session.context.new_page()
        else:
            raise
    return session._page


async def safe_recycle_tab_db(session: Any) -> Any:
    """Safely rotate tab: open new page before closing old one."""
    if is_context_closed(session.context):
        await session.restart()
    assert session.context is not None
    try:
        new_page = await session.context.new_page()
        old_page = session._page
        session._page = new_page
        session._nav_count = 0
        if old_page is not None:
            try:
                if not old_page.is_closed():
                    await old_page.close()
            except Exception as exc:
                logger.debug("Error closing old page during tab recycle: {}", exc)
        logger.info("Browser tab recycled safely")
        return new_page
    except Exception as exc:
        logger.warning("Error recycling tab: {}", exc)
        if is_target_closed_error(exc):
            logger.warning("TargetClosedError in _safe_recycle_tab. Restarting browser...")
            await session.restart()
        return await session._get_or_create_page()
