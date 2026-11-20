"""Deferred HTTP work skips scheduled windows, without a catch-up queue."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from hunter.application.slot_worker import SlotWorker
from hunter.transport.metered_http import HttpDeferred


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def wall(self):
        return datetime(2026, 11, 4, 12, tzinfo=UTC) + timedelta(seconds=self.value)

    async def sleep(self, seconds):
        self.value += seconds


@pytest.mark.asyncio
async def test_deferral_skips_all_slot_windows_until_deadline_without_catchup():
    clock, calls = Clock(), []

    async def poll(query, sequence):
        calls.append((query, clock.value))
        if len(calls) == 1:
            raise HttpDeferred("cooldown", 40)

    worker = SlotWorker("slot", ["a", "b", "c", "d"], clock=clock.now,
                        wall_clock=clock.wall, sleep_func=clock.sleep, poll_handler=poll)
    await worker.run_deterministic_cycles(3)
    assert calls == [("a", 0), ("c", 45), ("d", 52.5),
                     ("a", 60), ("b", 67.5), ("c", 75), ("d", 82.5)]
    assert worker.deferred_count == 1
    assert worker.deferred_skip_count == 5


@pytest.mark.asyncio
async def test_success_cannot_shorten_existing_defer_deadline():
    clock = Clock()

    async def poll(query, sequence):
        raise HttpDeferred("cooldown", 100)

    worker = SlotWorker("slot", ["a"], clock=clock.now, wall_clock=clock.wall,
                        sleep_func=clock.sleep, poll_handler=poll)
    await worker.run_deterministic_cycles(2)
    assert len(worker.executed_queries) == 1
    assert worker.deferred_skip_count == 1


@pytest.mark.asyncio
async def test_stop_while_waiting_cannot_start_another_poll():
    clock, calls = Clock(), []
    worker = SlotWorker("slot", ["a", "b"], clock=clock.now, wall_clock=clock.wall,
                        poll_handler=lambda q, seq: record(calls, q))

    async def stop_during_wait(seconds):
        await clock.sleep(seconds)
        await worker.stop()

    worker.sleep_func = stop_during_wait
    await worker.run_deterministic_cycles(1)
    assert calls == ["a"]


async def record(calls, query):
    calls.append(query)


@pytest.mark.asyncio
async def test_poll_cancellation_is_not_a_deferral():
    async def cancel(query, sequence):
        raise asyncio.CancelledError()

    worker = SlotWorker("slot", ["a"], poll_handler=cancel)
    with pytest.raises(asyncio.CancelledError):
        await worker._execute_poll("a", 0)
