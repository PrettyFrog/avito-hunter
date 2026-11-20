"""Pause and policy changes during a scheduled wait must be observed."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hunter.application.slot_worker import SlotWorker


class Clock:
    def __init__(self, wall: datetime) -> None:
        self.wall_time = wall
        self.mono = 0.0
        self.paused = False

    def now(self) -> float:
        return self.mono

    def wall(self) -> datetime:
        return self.wall_time

    async def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.wall_time += timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_pause_started_while_waiting_skips_poll() -> None:
    clock = Clock(datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Europe/Moscow")))
    calls: list[str] = []

    async def sleep_and_pause(seconds: float) -> None:
        await clock.sleep(seconds)
        clock.paused = True

    async def poll(query_id: str, sequence: int) -> None:
        calls.append(query_id)

    worker = SlotWorker("slot", ["a", "b"], clock=clock.now, wall_clock=clock.wall,
                        sleep_func=sleep_and_pause, is_paused=lambda: clock.paused,
                        poll_handler=poll)
    await worker.run_deterministic_cycles(1)
    assert calls == ["a"]


@pytest.mark.asyncio
async def test_night_start_during_wait_rebases_remaining_offset() -> None:
    clock = Clock(datetime(2026, 10, 25, 0, 59, 59, tzinfo=ZoneInfo("Europe/Moscow")))
    calls: list[tuple[str, float]] = []

    async def poll(query_id: str, sequence: int) -> None:
        calls.append((query_id, clock.mono))

    worker = SlotWorker("slot", ["a", "b"], clock=clock.now, wall_clock=clock.wall,
                        sleep_func=clock.sleep, poll_handler=poll)
    await worker.run_deterministic_cycles(1)
    assert calls[0] == ("a", 0.0)
    assert calls[1][0] == "b"
    assert calls[1][1] >= 90.0


@pytest.mark.asyncio
async def test_single_query_does_not_repeat_at_day_to_night_boundary() -> None:
    clock = Clock(datetime(2026, 10, 25, 0, 59, 59, tzinfo=ZoneInfo("Europe/Moscow")))
    calls: list[float] = []

    async def poll(query_id: str, sequence: int) -> None:
        calls.append(clock.mono)

    worker = SlotWorker("slot", ["a"], clock=clock.now, wall_clock=clock.wall,
                        sleep_func=clock.sleep, poll_handler=poll)
    await worker.run_deterministic_cycles(2)
    assert calls == [0.0, 180.0]


@pytest.mark.asyncio
async def test_day_start_during_wait_uses_day_offset() -> None:
    clock = Clock(datetime(2026, 10, 25, 6, 59, 59, tzinfo=ZoneInfo("Europe/Moscow")))
    calls: list[float] = []

    async def poll(query_id: str, sequence: int) -> None:
        calls.append(clock.mono)

    worker = SlotWorker("slot", ["a", "b"], clock=clock.now, wall_clock=clock.wall,
                        sleep_func=clock.sleep, poll_handler=poll)
    await worker.run_deterministic_cycles(1)
    assert calls == [0.0, 15.0]


@pytest.mark.asyncio
async def test_generation_change_during_wait_skips_old_owner() -> None:
    clock = Clock(datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Europe/Moscow")))
    calls: list[str] = []
    worker: SlotWorker

    async def replace_owner(seconds: float) -> None:
        await clock.sleep(seconds)
        worker.generation += 1

    async def poll(query_id: str, sequence: int) -> None:
        calls.append(query_id)

    worker = SlotWorker("slot", ["a", "b"], clock=clock.now, wall_clock=clock.wall,
                        sleep_func=replace_owner, poll_handler=poll)
    await worker.run_deterministic_cycles(1)
    assert calls == ["a"]


@pytest.mark.asyncio
async def test_async_pause_callback_false_does_not_skip_poll() -> None:
    calls: list[str] = []

    async def paused() -> bool:
        return False

    async def poll(query_id: str, sequence: int) -> None:
        calls.append(query_id)

    worker = SlotWorker("slot", ["query"], is_paused=paused, poll_handler=poll)
    worker._running = True
    await worker.run_cycle(worker.clock())
    worker._running = False
    assert calls == ["query"]
    assert worker.pause_skip_count == 0
