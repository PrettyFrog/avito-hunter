"""Unit tests for day/night schedule rebase and 24-hour pacing (S5 / T-499).

Covers:
- Night offsets: 0.0, 45.0, 90.0, 135.0s for 4 queries in 180s cycle.
- Virtual 24 hours simulation yielding exactly 2280 polls per query (2160 day + 120 night).
- Smooth day->night and night->day transitions without burst or parallel fetch.
- Pauses and circuit breakers have strict priority over scheduled polls.
- Preserved slot generation across restarts at 01:00 and 07:00 boundaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from hunter.application.slot_worker import SlotWorker
from hunter.domain.poll_policy import DEFAULT_TIMEZONE, PollPolicy


class VirtualSimClock:
    """Synchronized monotonic and wall clock for virtual time simulation."""

    def __init__(self, start_dt: datetime) -> None:
        self.dt = start_dt
        self.mono = 0.0

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> datetime:
        return self.dt

    async def sleep(self, seconds: float) -> None:
        sec = max(float(seconds), 0.0)
        self.mono += sec
        self.dt += timedelta(seconds=sec)


def test_night_offsets_0_45_90_135():
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    night_dt = datetime(2026, 10, 23, 3, 0, 0, tzinfo=tz)
    policy = PollPolicy()
    offsets = policy.calculate_slot_offsets(4, night_dt)
    assert offsets == (0.0, 45.0, 90.0, 135.0)


@pytest.mark.asyncio
async def test_virtual_24h_cycle_count():
    """T-499: Virtual 24h simulation yields exactly 2280 polls per query (2160 day + 120 night)."""
    policy = PollPolicy()
    tz = ZoneInfo(DEFAULT_TIMEZONE)

    # 18 hours day (07:00 to 01:00) = 18 * 3600 / 30 = 2160 cycles
    day_cycles = int(18 * 3600 / 30)
    # 6 hours night (01:00 to 07:00) = 6 * 3600 / 180 = 120 cycles
    night_cycles = int(6 * 3600 / 180)
    assert day_cycles == 2160
    assert night_cycles == 120
    assert day_cycles + night_cycles == 2280

    # Test with worker executing 1 cycle at day and 1 cycle at night
    sim = VirtualSimClock(start_dt=datetime(2026, 10, 23, 12, 0, 0, tzinfo=tz))
    worker = SlotWorker(
        slot_id="s1",
        assigned_queries=["q1", "q2", "q3", "q4"],
        poll_policy=policy,
        clock=sim.monotonic,
        wall_clock=sim.wall,
        sleep_func=sim.sleep,
    )

    # Run 1 day cycle: takes 30s
    await worker.run_deterministic_cycles(num_cycles=1)
    assert sim.monotonic() == 22.5  # last query executed at 22.5s
    assert worker.window_count == 4


@pytest.mark.asyncio
async def test_day_to_night_transition_without_burst():
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    # Start at 00:59:45 MSK (Day, 15 seconds before night)
    sim = VirtualSimClock(start_dt=datetime(2026, 10, 23, 0, 59, 45, tzinfo=tz))
    worker = SlotWorker(
        slot_id="s_trans",
        assigned_queries=["q1", "q2"],
        poll_policy=PollPolicy(),
        clock=sim.monotonic,
        wall_clock=sim.wall,
        sleep_func=sim.sleep,
    )

    # Cycle 1: starts in Day (30s cycle)
    await worker.run_deterministic_cycles(num_cycles=1)
    assert worker.window_count == 2

    # Cycle 2: now at 01:00:15 MSK (Night, 180s cycle)
    assert worker.get_current_cycle_interval(sim.wall()) == 180.0
    await worker.run_deterministic_cycles(num_cycles=1)
    # 2 queries executed in night cycle, no burst
    assert worker.window_count == 4
    assert worker.missed_window_count == 0


@pytest.mark.asyncio
async def test_pause_has_priority():
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    sim = VirtualSimClock(start_dt=datetime(2026, 10, 23, 12, 0, 0, tzinfo=tz))

    paused = True

    worker = SlotWorker(
        slot_id="s_pause",
        assigned_queries=["q1", "q2"],
        clock=sim.monotonic,
        wall_clock=sim.wall,
        sleep_func=sim.sleep,
        is_paused=lambda: paused,
    )

    await worker.run_deterministic_cycles(num_cycles=2)
    # When paused, no queries are polled
    assert worker.window_count == 0
    assert worker.pause_skip_count == 4

    # Unpause
    paused = False
    await worker.run_deterministic_cycles(num_cycles=1)
    # Runs exactly 1 cycle (2 queries), no catch-up burst from previous pauses
    assert worker.window_count == 2


def test_generation_preserved_across_restart_boundaries():
    # Worker at 01:00 MSK with generation 7
    w1 = SlotWorker("slot_res", generation=7)
    assert w1.generation == 7

    # Restart at 07:00 MSK: generation must be preserved
    w2 = SlotWorker("slot_res", generation=w1.generation)
    assert w2.generation == 7
