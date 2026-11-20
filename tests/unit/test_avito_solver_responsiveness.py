"""Unit tests for CPU responsiveness, cooperative cancellation, and event-loop heartbeat (T-979)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from hunter.collect.firewall_pow import find_nonce, solve_firewall_pow_http
from hunter.transport.pow_execution import get_pow_executor, run_cpu_bound_cooperative


@pytest.mark.asyncio
async def test_event_loop_heartbeat_not_blocked_during_pow() -> None:
    """Event loop must remain responsive and tick heartbeat while PoW computation runs."""
    heartbeat_ticks = 0
    stop_heartbeat = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat.is_set():
            await asyncio.sleep(0.005)
            heartbeat_ticks += 1

    hb_task = asyncio.create_task(_heartbeat())

    def slow_nonce(challenge_id: str, complexity: int = 4, max_iter: int = 10_000_000, stop_event: Any = None) -> int:
        time.sleep(0.05)
        return 42

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/get"):
            return httpx.Response(200, json={"success": {"result": {"challenge_jwt": "e30.eyJpZCI6ICJhIiwgImNvbXBsIjogMX0=.sig"}}})
        if req.url.path.endswith("/verify"):
            return httpx.Response(200, json={"success": {"result": {"verified": True}}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with patch("hunter.collect.firewall_pow.find_nonce", slow_nonce):
            solved, _ = await solve_firewall_pow_http("test-token", http_client=client)

    stop_heartbeat.set()
    await hb_task

    assert solved is True
    assert heartbeat_ticks >= 3, f"Event loop was blocked! Heartbeat ticks: {heartbeat_ticks}"


@pytest.mark.asyncio
async def test_cooperative_stop_on_timeout() -> None:
    """CPU calculation must stop promptly when timeout expires, not continue spinning."""
    computed_steps = 0

    def endless_calc(stop_event: Any = None) -> int:
        nonlocal computed_steps
        for _ in range(1_000_000):
            if stop_event and stop_event.is_set():
                break
            time.sleep(0.001)
            computed_steps += 1
        return computed_steps

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await run_cpu_bound_cooperative(endless_calc, timeout_sec=0.02)

    elapsed = time.monotonic() - started
    assert elapsed < 0.05, f"Timeout took too long: {elapsed:.3f}s"

    snapshot_steps = computed_steps
    await asyncio.sleep(0.03)
    assert computed_steps - snapshot_steps <= 2, (
        f"Worker kept computing after timeout! Steps grew from {snapshot_steps} to {computed_steps}"
    )


def test_pow_executor_bounded_workers() -> None:
    """PoW executor must enforce a strict bounded worker pool."""
    executor = get_pow_executor()
    max_workers = getattr(executor, "_max_workers", 0)
    assert 1 <= max_workers <= 4, f"Executor max_workers out of bounds: {max_workers}"


@pytest.mark.asyncio
async def test_executor_admission_queue_bounded() -> None:
    """Admission control must prevent unbounded thread pool work queue overflow."""
    release = threading.Event()

    def busy(stop_event: Any = None) -> int:
        while not release.is_set() and not (stop_event and stop_event.is_set()):
            time.sleep(0.001)
        return 0

    tasks = [asyncio.create_task(run_cpu_bound_cooperative(busy)) for _ in range(12)]
    try:
        await asyncio.sleep(0.03)
        executor = get_pow_executor()
        assert executor._work_queue.qsize() == 0, f"Work queue not bounded: {executor._work_queue.qsize()}"
    finally:
        for task in tasks:
            task.cancel()
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.02)

