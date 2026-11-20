"""Unit tests for sequential SlotWorker with monotonic schedule (S5 / T-481).

Covers:
- Fake clock pacing at 0.0, 7.5, 15.0, 22.5s offsets in 30s cycle.
- Exact window counting across multiple cycles.
- Slow HTTP response / missed deadline without catch-up burst.
- Clean cancellation of the worker task.
- Verification that legacy priority intervals remain unchanged.
"""

from __future__ import annotations

import pytest

from hunter.application.slot_worker import SlotWorker
from hunter.core.config import Settings
from hunter.domain.poll_policy import PollPolicy


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.current = start

    def monotonic(self) -> float:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += max(float(seconds), 0.0)


@pytest.mark.asyncio
async def test_fake_clock_offsets_0_7_5_15_22_5():
    fake = FakeClock(start=0.0)
    worker = SlotWorker(
        slot_id="slot_01",
        assigned_queries=["q0", "q1", "q2", "q3"],
        cycle_interval_sec=30.0,
        poll_policy=PollPolicy(day_interval_sec=30.0, night_interval_sec=30.0),
        clock=fake.monotonic,
        sleep_func=fake.sleep,
    )

    await worker.run_deterministic_cycles(num_cycles=1)

    assert worker.window_count == 4
    assert worker.missed_window_count == 0
    assert len(worker.executed_queries) == 4

    # Verification of exact offsets: 0.0, 7.5, 15.0, 22.5s
    times = [entry[2] for entry in worker.executed_queries]
    assert times == [0.0, 7.5, 15.0, 22.5]
    # Verification of monotonic request sequence: 1, 2, 3, 4
    seqs = [entry[1] for entry in worker.executed_queries]
    assert seqs == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_exact_window_count_across_multiple_cycles():
    fake = FakeClock(start=100.0)
    worker = SlotWorker(
        slot_id="slot_02",
        assigned_queries=["qa", "qb", "qc", "qd"],
        cycle_interval_sec=30.0,
        poll_policy=PollPolicy(day_interval_sec=30.0, night_interval_sec=30.0),
        clock=fake.monotonic,
        sleep_func=fake.sleep,
    )

    # 3 full cycles of 30s with 4 queries = exactly 12 windows
    await worker.run_deterministic_cycles(num_cycles=3)

    assert worker.window_count == 12
    assert worker.sequence == 12
    assert worker.missed_window_count == 0


@pytest.mark.asyncio
async def test_slow_http_missed_deadline_no_catchup_burst():
    fake = FakeClock(start=0.0)

    async def slow_poll_handler(query_id: str, sequence: int) -> None:
        if query_id == "q0":
            # Simulate slow HTTP fetch taking 10 seconds (exceeds q1's 7.5s deadline)
            fake.current += 10.0

    worker = SlotWorker(
        slot_id="slot_slow",
        assigned_queries=["q0", "q1", "q2", "q3"],
        cycle_interval_sec=30.0,
        poll_policy=PollPolicy(day_interval_sec=30.0, night_interval_sec=30.0),
        poll_handler=slow_poll_handler,
        clock=fake.monotonic,
        sleep_func=fake.sleep,
    )

    await worker.run_deterministic_cycles(num_cycles=1)

    # q0 ran, took 10s. q1 at 7.5s was missed!
    assert worker.missed_window_count == 1
    # q2 ran at 15.0s, q3 ran at 22.5s
    executed_ids = [entry[0] for entry in worker.executed_queries]
    assert executed_ids == ["q0", "q2", "q3"]
    # No catch-up burst: total executed is 3 (missed 1)
    assert worker.window_count == 3


@pytest.mark.asyncio
async def test_clean_cancellation():
    worker = SlotWorker(
        slot_id="slot_cancel",
        assigned_queries=["q1"],
        cycle_interval_sec=30.0,
    )

    await worker.start()
    assert worker._running is True

    # Stop cleanly
    await worker.stop()
    assert worker._running is False
    assert worker._task is None


def test_legacy_intervals_unchanged():
    settings = Settings()
    # Legacy default priority intervals are strictly preserved: high 15, normal 30, low 60
    assert settings.priority_intervals["high"] == 15
    assert settings.priority_intervals["normal"] == 30
    assert settings.priority_intervals["low"] == 60
