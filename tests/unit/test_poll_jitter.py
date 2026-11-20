"""Unit tests for Public V2 poll interval jitter and maximum jitter cap."""

from __future__ import annotations

import random
from datetime import UTC, datetime
import pytest

from hunter.core.config import PublicV2Config
from hunter.domain.poll_policy import PollPolicy
from hunter.application.slot_worker import SlotWorker


def test_public_v2_config_jitter_fields():
    cfg = PublicV2Config()
    assert hasattr(cfg, "jitter_ratio")
    assert hasattr(cfg, "max_jitter_sec")
    assert cfg.jitter_ratio == 0.25
    assert cfg.max_jitter_sec == 180.0


def test_poll_policy_jitter_calculation_and_cap():
    # 1. 5s interval with 0.25 jitter -> delta <= 1.25s
    policy = PollPolicy(
        day_interval_sec=5.0,
        night_interval_sec=5.0,
        jitter_ratio=0.25,
        max_jitter_sec=180.0,
    )
    samples = [policy.get_jittered_cycle_interval() for _ in range(100)]
    assert all(3.75 <= s <= 6.25 for s in samples)
    # Ensure there is actual randomness (not all identical)
    assert len(set(samples)) > 10

    # 2. Large interval (1000s) -> 0.25 * 1000 = 250s, but capped at max_jitter_sec=180s
    policy_large = PollPolicy(
        day_interval_sec=1000.0,
        night_interval_sec=1000.0,
        jitter_ratio=0.25,
        max_jitter_sec=180.0,
    )
    samples_large = [policy_large.get_jittered_cycle_interval() for _ in range(100)]
    assert all(820.0 <= s <= 1180.0 for s in samples_large)
    assert all(s >= 820.0 for s in samples_large)
    assert all(s <= 1180.0 for s in samples_large)


def test_poll_policy_deterministic_injection():
    # Inject deterministic random_func: returns min (-max_delta) or max (+max_delta)
    policy_min = PollPolicy(
        day_interval_sec=10.0,
        night_interval_sec=10.0,
        jitter_ratio=0.2,
        max_jitter_sec=180.0,
        random_func=lambda low, high: low,
    )
    assert policy_min.get_jittered_cycle_interval() == 8.0

    policy_max = PollPolicy(
        day_interval_sec=10.0,
        night_interval_sec=10.0,
        jitter_ratio=0.2,
        max_jitter_sec=180.0,
        random_func=lambda low, high: high,
    )
    assert policy_max.get_jittered_cycle_interval() == 12.0


@pytest.mark.asyncio
async def test_slot_worker_cadence_with_jitter():
    class FakeClock:
        def __init__(self) -> None:
            self.current = 100.0

        def monotonic(self) -> float:
            return self.current

        async def sleep(self, seconds: float) -> None:
            self.current += max(float(seconds), 0.0)

    fake = FakeClock()
    policy = PollPolicy(
        day_interval_sec=5.0,
        night_interval_sec=5.0,
        jitter_ratio=0.25,
        max_jitter_sec=180.0,
    )
    worker = SlotWorker(
        slot_id="slot_jitter",
        assigned_queries=["q1"],
        poll_policy=policy,
        clock=fake.monotonic,
        sleep_func=fake.sleep,
    )

    await worker.run_deterministic_cycles(num_cycles=5)
    assert len(worker.executed_queries) == 5

    timestamps = [q[2] for q in worker.executed_queries]
    intervals = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    # All intervals must be within [3.75, 6.25]
    assert all(3.75 <= dt <= 6.25 for dt in intervals)
    # Consecutive intervals should not all be identical
    assert len(set(round(dt, 4) for dt in intervals)) > 1
