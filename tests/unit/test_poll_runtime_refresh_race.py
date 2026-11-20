from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy


@pytest.fixture
def fake_runtime():
    pool = MagicMock()
    fetcher = MagicMock()
    fetcher.load_cookies_from_store = MagicMock(return_value=None)
    pool.get_fetcher = MagicMock(return_value=fetcher)
    runtime = PublicPollRuntime(
        conn_mgr=None,
        http_pool=pool,
        ingest=AsyncMock(),
        policy=PollPolicy(1, 1),
    )
    return runtime


@pytest.mark.asyncio
async def test_stale_snapshot_does_not_overwrite_newer_generation(fake_runtime):
    """T-956: An older assignment snapshot must not overwrite a newer generation."""
    newer_asg = QueryAssignment("slot_1", "direct", "q_1", "https://avito.ru/q1", 5, 0)
    fake_runtime.assignments[("slot_1", "q_1")] = newer_asg

    stale_asg = QueryAssignment("slot_1", "direct", "q_1", "https://avito.ru/q1", 3, 0)
    await fake_runtime._replace_slot("slot_1", [stale_asg], start=False)

    # Newer generation 5 must be preserved
    assert fake_runtime.assignments[("slot_1", "q_1")].generation == 5


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_block_stop(fake_runtime):
    """T-956: Cancellation of a refresh waiter must not block or break shutdown."""
    # Hold the lock artificially
    await fake_runtime._refresh_lock.acquire()

    waiter = asyncio.create_task(fake_runtime.refresh(start=False))
    await asyncio.sleep(0.01)

    # Cancel the waiter while it is blocked on lock
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    # Release artificial lock hold
    fake_runtime._refresh_lock.release()

    # stop() must complete cleanly without blocking
    await asyncio.wait_for(fake_runtime.stop(), timeout=0.5)
    assert fake_runtime._is_stopped is True


@pytest.mark.asyncio
async def test_refresh_serialized_with_lock(fake_runtime):
    """T-956: refresh acquires lock ensuring sequential execution."""
    execution_order = []

    async def fake_load_active_assignments(_conn_mgr):
        execution_order.append("load_start")
        await asyncio.sleep(0.02)
        execution_order.append("load_done")
        return []

    from unittest.mock import patch
    with patch("hunter.application.public_poll_runtime.load_active_assignments", fake_load_active_assignments):
        t1 = asyncio.create_task(fake_runtime.refresh(start=False))
        t2 = asyncio.create_task(fake_runtime.refresh(start=False))
        await asyncio.gather(t1, t2)

    assert execution_order == ["load_start", "load_done", "load_start", "load_done"]


@pytest.mark.asyncio
async def test_cancelled_stop_does_not_resurrect_workers_after_refresh(fake_runtime):
    """T-956: When stop is cancelled during refresh, refresh must not publish new workers."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def fake_load(_conn_mgr):
        entered.set()
        await release.wait()
        return [QueryAssignment("slot_1", "direct", "q_1", "https://avito.ru/q1", 1, 0)]

    from unittest.mock import patch
    with patch("hunter.application.public_poll_runtime.load_active_assignments", fake_load):
        t_ref = asyncio.create_task(fake_runtime.refresh(start=False))
        await entered.wait()
        t_stop = asyncio.create_task(fake_runtime.stop())
        await asyncio.sleep(0.01)
        t_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t_stop
        release.set()
        await t_ref

    assert fake_runtime._is_stopped is True
    assert len(fake_runtime.workers) == 0
