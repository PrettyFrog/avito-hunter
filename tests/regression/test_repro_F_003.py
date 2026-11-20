"""Regression test for F-003: SlotWorker._loop dies permanently on unhandled poll exception."""

from __future__ import annotations

import asyncio
import pytest

from hunter.application.slot_worker import SlotWorker
from hunter.domain.poll_policy import PollPolicy


@pytest.mark.asyncio
async def test_repro_F_003_slot_worker_survives_transient_poll_exception() -> None:
    call_count = 0

    async def crashing_poll(query_id: str, seq: int) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Transient database lock or parse error")

    worker = SlotWorker(
        "slot_1", ["q1", "q2", "q3"],
        poll_policy=PollPolicy(day_interval_sec=0.01, night_interval_sec=0.01),
        poll_handler=crashing_poll,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.08)
        # Expected behavior: worker should survive transient error and continue running
        assert worker._task is not None
        assert not worker._task.done(), f"Worker crashed permanently: {worker._task.exception()!r} (F-003)"
        assert call_count > 2, f"Worker stopped polling after failure, calls: {call_count}"
    finally:
        await worker.stop()
