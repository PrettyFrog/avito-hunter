"""Politeness before_request helper functions (part 1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from hunter.core.models import now_utc


class CircuitOpen(Exception):
    """Collector paused itself after blocks/rate limits."""


async def check_circuit_and_budget(
    db: Any,
    politeness: Any,
    now_utc_fn: Any = None,
) -> None:
    if now_utc_fn is None:
        now_utc_fn = now_utc
    if await db.is_paused():
        raise CircuitOpen("paused")
    until_str = await db.get_runtime("circuit_until")
    if until_str:
        try:
            until_dt = datetime.fromisoformat(until_str)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=UTC)
            if until_dt > now_utc_fn():
                raise CircuitOpen(f"circuit until {until_dt.isoformat()}")
        except (ValueError, TypeError):
            pass
    # DECISION: boundary of request budget day (UTC vs Europe/Moscow midnight) remains UTC for now
    used = await db.today_requests()
    daily_cap = await db.get_daily_cap()
    if used >= daily_cap:
        raise CircuitOpen("daily budget exhausted")
    val = await db.get_runtime("budget_night_enabled")
    if val is not None and val != "":
        politeness._night_enabled = val in ("1", "true", "True")
    elif val == "":
        politeness._night_enabled = None


def calculate_target_wait(
    settings: Any,
    is_light: bool,
    is_night: bool,
    delay_fn: Any,
) -> float:
    budget = settings.request_budget
    min_delay = float(budget.min_delay_seconds)
    max_delay = float(budget.max_delay_seconds)
    if is_light and getattr(settings, "collect", None):
        l_min = getattr(settings.collect, "light_min_delay_seconds", None)
        l_max = getattr(settings.collect, "light_max_delay_seconds", None)
        if isinstance(l_min, (int, float)) and not isinstance(l_min, bool):
            min_delay = float(l_min)
        if isinstance(l_max, (int, float)) and not isinstance(l_max, bool):
            max_delay = float(l_max)

    night_mult = float(getattr(budget, "night_multiplier", 3.2))
    return float(
        delay_fn(
            min_delay,
            max_delay,
            is_night=is_night,
            night_multiplier=night_mult,
        )
    )


def compute_profile_earliest(
    politeness: Any,
    prof_key: str,
    target_wait: float,
    now_mono: float,
) -> tuple[float, float]:
    last_prof_finish = politeness._last_profile_finish.get(prof_key, 0.0)
    if last_prof_finish > 0.0:
        elapsed = now_mono - last_prof_finish
        prof_earliest = last_prof_finish + target_wait
    else:
        last_prof_start = politeness._last_profile_request.get(prof_key, 0.0)
        if (
            last_prof_start == 0.0
            and prof_key == "profile_0"
            and politeness._last_request_start > 0.0
        ):
            last_prof_start = politeness._last_request_start
        if last_prof_start > 0.0:
            elapsed = now_mono - last_prof_start
            prof_earliest = last_prof_start + target_wait
        else:
            elapsed = 0.0
            prof_earliest = now_mono
    return prof_earliest, elapsed


def schedule_request_slot(
    politeness: Any,
    settings: Any,
    prof_key: str,
    prof_earliest: float,
    now_mono: float,
) -> float:
    global_gap = 0.0
    if getattr(settings, "collect", None) is not None:
        raw_gap = getattr(settings.collect, "global_min_gap_seconds", 1.0)
        if isinstance(raw_gap, (int, float)) and not isinstance(raw_gap, bool):
            global_gap = max(0.0, float(raw_gap))

    # Cross-profile or cross-request minimum spacing to avoid concurrent burst on single IP
    if politeness._last_finish_time > 0.0 and global_gap > 0.0:
        global_earliest = politeness._last_finish_time + global_gap
    elif (
        politeness._last_global_request > 0.0
        and politeness._last_global_profile != prof_key
        and global_gap > 0.0
    ):
        global_earliest = politeness._last_global_request + global_gap
    else:
        global_earliest = now_mono

    scheduled_start = max(now_mono, prof_earliest, global_earliest)
    wait_sec = max(0.0, scheduled_start - now_mono)

    # Reserve slot inside lock so concurrent coroutines see the scheduled start immediately
    politeness._last_profile_request[prof_key] = scheduled_start
    politeness._last_global_request = scheduled_start
    politeness._last_global_profile = prof_key
    politeness._last_request_start = scheduled_start

    return wait_sec


async def execute_politeness_wait(
    wait_sec: float,
    prof_key: str,
    target_wait: float,
    elapsed: float,
    sleep_fn: Any = None,
) -> None:
    if sleep_fn is None:
        sleep_fn = asyncio.sleep
    if wait_sec > 0.0:
        logger.debug(
            "politeness wait {:.1f}s (profile={}, target={:.1f}s, elapsed={:.1f}s)",
            wait_sec,
            prof_key,
            target_wait,
            elapsed,
        )
        await sleep_fn(wait_sec)
    else:
        logger.debug(
            "politeness wait skipped (profile={}, elapsed={:.1f}s >= target={:.1f}s)",
            prof_key,
            elapsed,
            target_wait,
        )
