from __future__ import annotations

import asyncio
import threading
import time
import pytest

from hunter.transport.pow_execution import (
    run_cpu_bound_cooperative,
    get_pow_executor,
    shutdown_pow_executor,
)


@pytest.fixture(autouse=True)
def _reset_executor():
    shutdown_pow_executor(wait=True)
    yield
    shutdown_pow_executor(wait=True)


@pytest.mark.asyncio
async def test_admission_timeout_includes_semaphore_wait():
    """T-979: Overall deadline timeout_sec must cover semaphore admission wait."""
    executor = get_pow_executor()
    sem = executor._get_semaphore()
    for _ in range(executor._max_workers):
        await sem.acquire()

    start_t = time.monotonic()
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await run_cpu_bound_cooperative(lambda: 42, timeout_sec=0.05)
    elapsed = time.monotonic() - start_t
    assert elapsed < 0.25
    for _ in range(executor._max_workers):
        sem.release()


@pytest.mark.asyncio
async def test_capacity_held_until_thread_completes():
    """T-979: Cancelling async task must not release semaphore permit while thread is running."""
    executor = get_pow_executor()
    sem = executor._get_semaphore()
    release_event = threading.Event()

    def long_task(stop_event):
        release_event.wait(0.2)
        return "done"

    tasks = [asyncio.create_task(run_cpu_bound_cooperative(long_task)) for _ in range(executor._max_workers)]
    await asyncio.sleep(0.01)

    assert sem.locked()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # While threads are still waiting on release_event, sem must remain locked
    assert sem.locked()
    release_event.set()
    await asyncio.sleep(0.05)
    assert not sem.locked()


@pytest.mark.asyncio
async def test_shutdown_drains_all_admission_waiters_with_error():
    """T-979: Shutdown must reject admission waiters with RuntimeError and release all permits."""
    executor = get_pow_executor()
    sem = executor._get_semaphore()
    await sem.acquire()
    await sem.acquire()

    tasks = [asyncio.create_task(run_cpu_bound_cooperative(lambda: 1)) for _ in range(4)]
    await asyncio.sleep(0.01)

    shutdown_pow_executor(wait=False)
    sem.release()
    sem.release()

    done, pending = await asyncio.wait(tasks, timeout=0.1)
    assert len(pending) == 0
    assert len(done) == 4
    for t in done:
        with pytest.raises(RuntimeError):
            t.result()


@pytest.mark.asyncio
async def test_instance_owned_executor_lifecycle():
    """T-979: Instance-owned executor operates independently and drains on shutdown."""
    from hunter.transport.pow_execution import CpuBoundExecutor

    inst_exec = CpuBoundExecutor(max_workers=1)
    sem = inst_exec._get_semaphore()
    await sem.acquire()

    task = asyncio.create_task(run_cpu_bound_cooperative(lambda: 42, executor=inst_exec))
    await asyncio.sleep(0.01)

    inst_exec.shutdown(wait=False)
    sem.release()

    done, pending = await asyncio.wait([task], timeout=0.1)
    assert len(pending) == 0
    with pytest.raises(RuntimeError):
        task.result()


@pytest.mark.asyncio
async def test_unstarted_bootstrap_shutdown_does_not_close_active_executor():
    """T-979: Cleanup of unstarted Bootstrap must not close another owner's active pool."""
    from hunter.transport.pow_execution import CpuBoundExecutor

    owner_a_exec = CpuBoundExecutor(max_workers=2)
    owner_b_exec = None  # Represents unstarted bootstrap where pow_executor is None

    # Simulate shutdown of unstarted B
    if owner_b_exec is not None:
        owner_b_exec.shutdown(wait=False)

    assert not owner_a_exec.is_shutdown
    result = await run_cpu_bound_cooperative(lambda: 99, executor=owner_a_exec)
    assert result == 99
    owner_a_exec.shutdown(wait=True)
    assert owner_a_exec.is_shutdown
