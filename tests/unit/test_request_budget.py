"""Deterministic shared admission; no real clocks, sleeps, network or cookies."""
import asyncio

import pytest

from hunter.domain.request_budget import RequestBudget


def test_same_egress_identities_share_budget_but_other_groups_do_not():
    budget = RequestBudget(enforce=True, spacing_sec=7.5, clock=lambda: 10)
    a = budget.before_attempt("shared", "avito", deadline=20)
    assert a.permit
    assert budget.before_attempt("shared", "avito", deadline=20).reason == "busy"
    assert budget.before_attempt("other", "avito", deadline=20).permit
    assert budget.before_attempt("shared", "other-service", deadline=20).permit
    assert budget.after_attempt(a.permit)
    assert not budget.after_attempt(a.permit)
    assert budget.before_attempt("shared", "avito", deadline=20).reason == "cooldown"


def test_shared_ip_mode_blocks_other_upstream_while_avito_is_active():
    budget = RequestBudget(enforce=True, spacing_sec=7.5, shared_upstreams=True,
                           clock=lambda: 10)
    first = budget.before_attempt("same-ip", "avito", deadline=30)
    assert first.permit
    assert budget.before_attempt("same-ip", "other", deadline=30).reason == "busy"
    assert budget.before_attempt("another-ip", "other", deadline=30).permit
    budget.after_attempt(first.permit, retry_after_sec=15)
    assert budget.before_attempt("same-ip", "other", deadline=30).not_before == 25


def test_observe_records_would_defer_without_suppressing_requests():
    budget = RequestBudget(clock=lambda: 10)
    a = budget.before_attempt(None, "avito", deadline=20)
    b = budget.before_attempt(None, "avito", deadline=20)
    assert a.permit and b.permit and a.permit != b.permit
    assert b.would_defer and not a.would_defer
    for permit in (a.permit, b.permit):
        assert budget.after_attempt(permit)
    assert budget.snapshot(None, "avito")["completed"] == 2


def test_expired_closed_or_over_capacity_does_not_reserve():
    budget = RequestBudget(max_active=1, clock=lambda: 10)
    assert budget.before_attempt(None, "avito", deadline=10).reason == "expired"
    permit = budget.before_attempt(None, "avito", deadline=20).permit
    assert budget.before_attempt("elsewhere", "avito", deadline=20).reason == "capacity"
    budget.close()
    assert budget.before_attempt(None, "avito", deadline=20).reason == "closed"
    assert budget.after_attempt(permit)
    assert budget.snapshot(None, "avito")["active"] == 0


def test_long_cooldown_and_no_catchup_reservations():
    now = [10.0]
    budget = RequestBudget(enforce=True, spacing_sec=7.5, clock=lambda: now[0])
    first = budget.before_attempt("ip", "avito", deadline=20).permit
    budget.after_attempt(first, retry_after_sec=9000)
    budget.restore_cooldown("ip", "avito", not_before=100)
    now[0] = 9009
    assert not budget.before_attempt("ip", "avito", deadline=9020).permit
    now[0] = 9010
    permitted = budget.before_attempt("ip", "avito", deadline=9020).permit
    assert permitted
    budget.after_attempt(permitted)
    assert not budget.before_attempt("ip", "avito", deadline=9020).permit


def test_max_cooldown_sec_clamps_retry_after():
    now = [10.0]
    budget = RequestBudget(enforce=True, max_cooldown_sec=902.58, clock=lambda: now[0])
    p = budget.before_attempt("ip", "avito", deadline=100).permit
    assert p
    budget.after_attempt(p, retry_after_sec=50000.0)
    assert budget.cooldown_deadline("ip", "avito") == 10.0 + 902.58


def test_live_day_night_spacing_changes_without_daily_quota():
    now = [10.0]
    spacing = [7.5]
    budget = RequestBudget(enforce=True, spacing_provider=lambda: spacing[0], clock=lambda: now[0])
    assert budget.has_spacing
    first = budget.before_attempt("ip", "avito", deadline=100).permit
    assert first
    budget.mark_sent(first)
    budget.after_attempt(first)
    now[0] = 18.0
    spacing[0] = 45.0
    second = budget.before_attempt("ip", "avito", deadline=100).permit
    assert second
    budget.mark_sent(second)
    budget.after_attempt(second)
    assert budget.before_attempt("ip", "avito", deadline=100).not_before == 63.0
    now[0] = 63.0
    spacing[0] = 7.5
    assert budget.before_attempt("ip", "avito", deadline=100).permit


@pytest.mark.asyncio
async def test_concurrent_admission_and_cancel_release():
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    started, stop = asyncio.Event(), asyncio.Event()
    async def hold():
        permit = budget.before_attempt("ip", "avito", deadline=20).permit
        assert permit
        started.set()
        try:
            await stop.wait()
        finally:
            budget.after_attempt(permit)
    task = asyncio.create_task(hold())
    await started.wait()
    async def contender():
        return budget.before_attempt("ip", "avito", deadline=20)
    decisions = await asyncio.gather(*(contender() for _ in range(100)))
    assert all(decision.permit is None for decision in decisions)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.before_attempt("ip", "avito", deadline=20).permit
