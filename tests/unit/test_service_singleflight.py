"""Coalescing is bounded, identity-specific and independent of individual waiters."""
import asyncio

import pytest

from hunter.transport.service_singleflight import ServiceSingleFlight


@pytest.mark.asyncio
async def test_one_cancelled_waiter_does_not_cancel_shared_operation():
    group = ServiceSingleFlight()
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    async def operation():
        calls.append(1)
        started.set()
        await release.wait()
        return "ready"
    async def request():
        return await group.run("slot", "user_b", 1, "warmup", operation)
    first, second = asyncio.create_task(request()), asyncio.create_task(request())
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == "ready" and calls == [1]
    await group.close()


@pytest.mark.asyncio
async def test_identity_generation_and_operation_are_not_coalesced():
    group = ServiceSingleFlight()
    calls = []
    async def operation():
        calls.append(1)
        await asyncio.sleep(0)
        return len(calls)
    await asyncio.gather(*[
        group.run("slot", identity, generation, name, operation)
        for identity, generation, name in [
            ("user_a", 1, "warmup"), ("user_b", 1, "warmup"),
            ("user_a", 2, "warmup"), ("user_a", 1, "refresh"),
        ]
    ])
    assert len(calls) == 4
    await group.close()


@pytest.mark.asyncio
async def test_capacity_shutdown_and_no_restart_after_stop():
    group = ServiceSingleFlight(capacity=1)
    started, stopped = asyncio.Event(), asyncio.Event()
    async def operation():
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
    first = asyncio.create_task(group.run("slot", "user_a", 1, "warmup", operation))
    await started.wait()
    with pytest.raises(RuntimeError, match="capacity"):
        await group.run("slot", "user_b", 1, "warmup", operation)
    await group.close()
    assert stopped.is_set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(RuntimeError, match="stopped"):
        await group.run("slot", "user_a", 1, "warmup", operation)


@pytest.mark.asyncio
async def test_failed_operation_is_not_cached_forever():
    group = ServiceSingleFlight()
    async def fail():
        raise OSError("synthetic failure")
    for _ in range(2):
        with pytest.raises(OSError):
            await group.run("slot", "user_a", 1, "warmup", fail)
    await group.close()
