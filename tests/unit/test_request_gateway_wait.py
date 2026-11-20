"""Configured spacing is a wait between sends; it is never a daily request quota."""
import asyncio
import time

import pytest

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import HttpDeferred, RequestGateway


@pytest.mark.asyncio
async def test_wait_respects_configured_interval():
    budget = RequestBudget(enforce=True, spacing_sec=0.03)
    gateway = RequestGateway(budget)
    args = dict(egress_group="shared", upstream="avito", deadline=time.monotonic() + 2)
    first = await gateway.acquire(**args)
    started = time.monotonic()
    budget.mark_sent(first)
    budget.after_attempt(first)
    second = await gateway.acquire(**args, wait=True)
    try:
        assert time.monotonic() - started >= 0.03
    finally:
        budget.after_attempt(second)


@pytest.mark.asyncio
async def test_waiter_capacity_cancellation_and_shutdown_are_bounded():
    budget = RequestBudget(enforce=True)
    gateway = RequestGateway(budget, max_waiters=1)
    args = dict(egress_group="shared", upstream="avito", deadline=time.monotonic() + 2)
    active = await gateway.acquire(**args)
    waiting = asyncio.create_task(gateway.acquire(**args, wait=True))
    try:
        while gateway.waiting != 1:
            await asyncio.sleep(0)
        with pytest.raises(HttpDeferred, match="admission_capacity"):
            await gateway.acquire(**args, wait=True)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert gateway.waiting == 0
        waiting = asyncio.create_task(gateway.acquire(**args, wait=True))
        while gateway.waiting != 1:
            await asyncio.sleep(0)
        budget.close()
        with pytest.raises(HttpDeferred, match="closed"):
            await waiting
        assert gateway.waiting == 0
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        budget.after_attempt(active)


@pytest.mark.asyncio
async def test_deadline_before_next_window_defers_without_sleeping():
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    budget.restore_cooldown("shared", "avito", not_before=100)
    gateway = RequestGateway(budget)
    with pytest.raises(HttpDeferred) as caught:
        await gateway.acquire(egress_group="shared", upstream="avito", deadline=20, wait=True)
    assert caught.value.not_before == 100
    assert gateway.waiting == 0


def test_interval_policy_continues_across_days_without_daily_quota():
    now = [0.0]
    budget = RequestBudget(enforce=True, spacing_sec=7.5, clock=lambda: now[0])
    for _ in range(23041):
        decision = budget.before_attempt("shared", "avito", deadline=now[0] + 1)
        assert decision.permit is not None
        budget.after_attempt(decision.permit)
        now[0] += 7.5
    assert now[0] > 2 * 86400
