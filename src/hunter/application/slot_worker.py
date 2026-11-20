"""Sequential single-task slot worker with monotonic schedule and day/night transitions (S5 / T-481, T-499).

Contracts:
- Exactly one worker task per slot: polls strictly execute sequentially, no concurrency overlap.
- Dynamic Day/Night pacing via PollPolicy:
  * Day: 30s cycle, offsets (0.0, 7.5, 15.0, 22.5s) for 4 queries.
  * Night: 180s cycle, offsets (0.0, 45.0, 90.0, 135.0s) for 4 queries.
- Epoch/monotonic deadlines: no drift, rebase on transitions without catch-up burst or parallel fetch.
- Pauses and circuit breakers have strict priority.
- 24h pacing yields exactly 2280 polls per query (2160 day + 120 night).
- Clean cancellation and preserved generation across restarts.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from hunter.transport.metered_http import HttpDeferred

from hunter.domain.poll_policy import (
    DAY_INTERVAL_SEC,
    MAX_SLOT_QUERIES,
    PollPolicy,
)


class SlotWorker:
    """Manages sequential request scheduling for a single slot across day/night boundaries."""

    def __init__(
        self,
        slot_id: str,
        assigned_queries: list[str] | None = None,
        cycle_interval_sec: float = DAY_INTERVAL_SEC,
        poll_policy: PollPolicy | None = None,
        poll_handler: Callable[[str, int], Awaitable[Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        is_paused: Callable[[], bool | Awaitable[bool]] | None = None,
        generation: int = 1,
    ) -> None:
        self.slot_id = slot_id
        self.assigned_queries: list[str] = list(assigned_queries or [])[:MAX_SLOT_QUERIES]
        self.cycle_interval_sec = float(cycle_interval_sec)
        self.poll_policy = poll_policy or PollPolicy()
        self.poll_handler = poll_handler
        self.clock = clock
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self.sleep_func = sleep_func
        self.is_paused = is_paused
        self.generation = int(generation)

        self._running = False
        self._task: asyncio.Task | None = None
        self._sequence = 0
        self.window_count = 0
        self.missed_window_count = 0
        self.pause_skip_count = 0
        self.deferred_count = 0
        self.deferred_skip_count = 0
        self._not_before = 0.0
        self.executed_queries: list[tuple[str, int, float]] = []

    async def _is_paused(self) -> bool:
        if self.is_paused is None:
            return False
        result = self.is_paused()
        return bool(await result) if inspect.isawaitable(result) else bool(result)

    def update_assigned_queries(self, queries: list[str]) -> None:
        """Replace the slot plan and fence the current cycle without restarting its task."""
        self.assigned_queries = list(queries)[:MAX_SLOT_QUERIES]
        self.generation += 1

    @property
    def sequence(self) -> int:
        return self._sequence

    def get_current_cycle_interval(self, now_dt: datetime | None = None, jitter: bool = False) -> float:
        dt = now_dt or self.wall_clock()
        if jitter and hasattr(self.poll_policy, "get_jittered_cycle_interval"):
            return self.poll_policy.get_jittered_cycle_interval(dt)
        return self.poll_policy.get_cycle_interval(dt)

    def calculate_offsets(self, now_dt: datetime | None = None) -> tuple[float, ...]:
        count = len(self.assigned_queries)
        if count == 0:
            return ()
        dt = now_dt or self.wall_clock()
        return self.poll_policy.calculate_slot_offsets(count, dt)

    async def _execute_poll(self, query_id: str, now_mono: float) -> None:
        self._sequence += 1
        seq = self._sequence
        self.window_count += 1
        self.executed_queries.append((query_id, seq, now_mono))
        if self.poll_handler:
            try:
                await self.poll_handler(query_id, seq)
            except asyncio.CancelledError:
                raise
            except HttpDeferred as deferred:
                self.deferred_count += 1
                self._not_before = max(self._not_before, deferred.not_before)
            except Exception as exc:
                logger.exception("Slot '{}' unhandled error polling query '{}': {}", self.slot_id, query_id, exc)

    async def _wait_for_offset(self, cycle_start_time: float, index: int) -> tuple[float, float]:
        """Wait to the current period's offset, waking at policy boundaries."""
        while True:
            now_dt = self.wall_clock()
            cycle_sec = self.get_current_cycle_interval(now_dt)
            offset = self.calculate_offsets(now_dt)[index]
            target = cycle_start_time + offset
            now_mono = self.clock()
            if now_mono >= target:
                return now_mono, cycle_sec
            boundary = self.poll_policy.seconds_until_transition(now_dt)
            await self.sleep_func(min(target - now_mono, boundary))

    async def run_cycle(self, cycle_start_time: float) -> float:
        """Executes a cycle across assigned query offsets monotonically without bursting."""
        generation = self.generation
        count = len(self.assigned_queries)

        for idx in range(count):
            if not self._running:
                break
            if generation != self.generation:
                break
            if await self._is_paused():
                self.pause_skip_count += 1
                continue

            now_mono, _ = await self._wait_for_offset(cycle_start_time, idx)
            if not self._running or generation != self.generation:
                break
            if await self._is_paused():
                self.pause_skip_count += 1
                continue

            # If previous request ran too long and missed this window deadline
            target_time = cycle_start_time + self.calculate_offsets()[idx]
            if now_mono > target_time + 1.0:
                self.missed_window_count += 1
                # No catch-up burst: skip to next window rather than bursting
                continue

            if now_mono < self._not_before:
                self.deferred_skip_count += 1
                continue
            query_id = self.assigned_queries[idx]
            await self._execute_poll(query_id, now_mono)

        return cycle_start_time + self.get_current_cycle_interval(jitter=True)

    async def _wait_for_next_cycle(self, cycle_start: float, deadline: float) -> float:
        """Rebase the next start if the day/night policy changes while waiting."""
        current_period = (
            self.poll_policy.get_period(self.wall_clock())
            if hasattr(self.poll_policy, "get_period")
            else None
        )
        while self._running and self.clock() < deadline:
            now_dt = self.wall_clock()
            if hasattr(self.poll_policy, "get_period"):
                new_period = self.poll_policy.get_period(now_dt)
                if current_period is not None and new_period != current_period:
                    current_period = new_period
                    deadline = cycle_start + self.get_current_cycle_interval(now_dt, jitter=True)
            if self.clock() >= deadline:
                break
            boundary = self.poll_policy.seconds_until_transition(now_dt)
            await self.sleep_func(min(deadline - self.clock(), boundary))
        return deadline

    async def _loop(self) -> None:
        next_cycle_time = self.clock()
        try:
            while self._running:
                now_mono = self.clock()
                interval = self.get_current_cycle_interval()
                if now_mono > next_cycle_time + interval:
                    # Rebase smoothly on big delays/pause without catch-up burst
                    next_cycle_time = now_mono
                cycle_start = next_cycle_time
                try:
                    next_cycle_time = await self.run_cycle(next_cycle_time)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Slot '{}' cycle error: {}", self.slot_id, exc)
                    next_cycle_time = cycle_start + self.get_current_cycle_interval(jitter=True)
                next_cycle_time = await self._wait_for_next_cycle(cycle_start, next_cycle_time)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        was_running = self._running
        self._running = False
        task = self._task
        if task is asyncio.current_task():
            return
        try:
            if task:
                if was_running and not task.done():
                    task.cancel()
                await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        finally:
            self._task = None

    async def run_deterministic_cycles(self, num_cycles: int) -> None:
        """Runs exact number of cycles for hermetic testing without background task."""
        self._running = True
        next_cycle_time = self.clock()
        cycle_start = next_cycle_time
        for idx in range(num_cycles):
            if not self._running:
                break
            if idx:
                next_cycle_time = await self._wait_for_next_cycle(cycle_start, next_cycle_time)
            cycle_start = next_cycle_time
            next_cycle_time = await self.run_cycle(next_cycle_time)
        self._running = False
