from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from zoneinfo import ZoneInfo

from loguru import logger

from hunter.collect.politeness_parts.politeness_before_request_01 import (
    CircuitOpen,
    calculate_target_wait,
    check_circuit_and_budget,
    compute_profile_earliest,
    execute_politeness_wait,
    schedule_request_slot,
)
from hunter.core.config import get_settings
from hunter.core.db import Database
from hunter.core.models import now_utc

__all__ = ["CircuitOpen", "Politeness", "next_human_delay"]


def next_human_delay(
    min_delay: float,
    max_delay: float,
    *,
    is_night: bool = False,
    night_multiplier: float = 3.2,
) -> float:
    """Generates human-like browsing delay with Gaussian distribution and diurnal profile.
    Dilutes gap_std and share_night_0_6 anti-bot CatBoost classifiers.
    """
    if max_delay <= min_delay:
        delay = float(min_delay)
    else:
        mean = (min_delay + max_delay) / 2.0
        sigma = (max_delay - min_delay) / 4.0
        delay = random.gauss(mean, sigma)
        # Clamp within natural bounds
        delay = max(min_delay * 0.75, min(delay, max_delay * 1.6))
        # 5% probability of natural user reading hesitation
        if random.random() < 0.05:
            delay += random.uniform(1.2, 2.8)

    if is_night:
        delay *= max(3.0, night_multiplier)

    return max(min_delay, round(delay, 2))


class Politeness:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = asyncio.Lock()
        self._profile_locks: dict[str, asyncio.Lock] = {}
        self._last_request_start: float = 0.0
        self._last_finish_time: float = 0.0
        self._last_profile_request: dict[str, float] = {}
        self._last_profile_finish: dict[str, float] = {}
        self._last_global_request: float = 0.0
        self._last_global_profile: str | None = None
        self._night_enabled: bool | None = None

    def _get_profile_lock(self, prof_key: str) -> asyncio.Lock:
        if prof_key not in self._profile_locks:
            self._profile_locks[prof_key] = asyncio.Lock()
        return self._profile_locks[prof_key]

    @asynccontextmanager
    async def request_gate(
        self,
        is_light: bool = False,
        profile_id: str | None = None,
    ) -> AsyncIterator[None]:
        """Serialize outbound requests against Avito for the given profile and enforce real post-request pause."""
        prof_key = profile_id or "profile_0"
        lock = self._get_profile_lock(prof_key)
        async with lock:
            await self.before_request(is_light=is_light, profile_id=profile_id)
            try:
                yield
            finally:
                self.record_finish(profile_id=profile_id)

    def record_finish(self, profile_id: str | None = None) -> None:
        """Record timestamp when an HTTP/browser request completed."""
        now_mono = time.monotonic()
        prof_key = profile_id or "profile_0"
        self._last_finish_time = max(self._last_finish_time, now_mono)
        self._last_profile_finish[prof_key] = max(
            self._last_profile_finish.get(prof_key, 0.0), now_mono
        )

    async def before_request(
        self,
        is_light: bool = False,
        profile_id: str | None = None,
    ) -> None:
        prof_key = profile_id or "profile_0"

        async with self._lock:
            settings = get_settings()
            await check_circuit_and_budget(self.db, self, now_utc_fn=now_utc)
            target_wait = calculate_target_wait(
                settings,
                is_light=is_light,
                is_night=self.is_night(),
                delay_fn=next_human_delay,
            )

            now_mono = time.monotonic()
            prof_earliest, elapsed = compute_profile_earliest(
                self,
                prof_key=prof_key,
                target_wait=target_wait,
                now_mono=now_mono,
            )
            wait_sec = schedule_request_slot(
                self,
                settings=settings,
                prof_key=prof_key,
                prof_earliest=prof_earliest,
                now_mono=now_mono,
            )

        await execute_politeness_wait(
            wait_sec=wait_sec,
            prof_key=prof_key,
            target_wait=target_wait,
            elapsed=elapsed,
            sleep_fn=asyncio.sleep,
        )

    def is_night(self) -> bool:
        if self._night_enabled is not None:
            if not self._night_enabled:
                return False
        else:
            settings = get_settings()
            budget = settings.request_budget
            if not getattr(budget, "night_enabled", True):
                return False
        settings = get_settings()
        budget = settings.request_budget
        start, end = budget.night_hours
        if start == end:
            return False
        tz_msk = ZoneInfo("Europe/Moscow")
        hour_msk = now_utc().astimezone(tz_msk).hour
        return start <= hour_msk < end

    async def on_status(
        self,
        status: int,
        profile_id: str | None = None,
        all_profiles_blocked: bool = True,
    ) -> None:
        self.record_finish(profile_id=profile_id)
        await self.db.bump_metric("requests")
        if status == 429:
            await self.db.bump_metric("http_429")
            if all_profiles_blocked:
                await self._backoff("429")
        elif status in {403, 401}:
            await self.db.bump_metric("http_403")
            if all_profiles_blocked:
                await self._backoff(str(status))
        elif status >= 500:
            if all_profiles_blocked:
                await self._backoff(f"http_{status}", minutes=10)

    async def _backoff(self, reason: str, minutes: int | None = None) -> None:
        settings = get_settings()
        if minutes is None:
            raw_min = getattr(getattr(settings, "request_budget", None), "circuit_minutes", 45)
            if isinstance(raw_min, (int, float)) and not isinstance(raw_min, bool):
                minutes = int(raw_min)
            else:
                minutes = 45
        until_dt = (now_utc() + timedelta(minutes=minutes)).replace(microsecond=0)
        await self.db.set_runtime("circuit_until", until_dt.isoformat())
        await self.db.set_runtime("circuit_reason", reason)
        logger.warning("circuit open for {} min after {}", minutes, reason)
        raise CircuitOpen(reason)
