"""A late header cannot invalidate or silently escape a native cooldown flush."""
import asyncio

import pytest

from hunter.domain.request_budget import RequestBudget
from hunter.transport.native_admission import NativeHopAdmission


def gate(now):
    budget = RequestBudget(enforce=True, clock=lambda: now[0])
    permit = budget.before_attempt("shared", "avito", deadline=10000).permit
    value = NativeHopAdmission(budget, permit, egress_group="shared", upstream="avito", deadline=10000)
    assert value.before_request() == 0
    return value


@pytest.mark.asyncio
async def test_new_upstream_header_during_flush_is_persisted_without_dict_race():
    value = gate([10.0])
    value.observe_header(b"Retry-After: 9000\r\n")
    entered, release = asyncio.Event(), asyncio.Event()
    saved = []
    class Store:
        async def extend(self, group, upstream, **kwargs):
            saved.append((upstream, kwargs["duration_sec"]))
            if len(saved) == 1:
                entered.set()
                await release.wait()
    task = asyncio.create_task(value.persist_cooldown(Store(), generation=1))
    try:
        await entered.wait()
        value.select_upstream("other")
        assert value.before_request() == 0
        value.observe_header(b"Retry-After: 120\r\n")
        release.set()
        await task
        assert saved == [("avito", 9000), ("other", 120)]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        value.close()


@pytest.mark.asyncio
async def test_later_floor_same_upstream_is_flushed_without_extending_receipt_time():
    now = [10.0]
    value = gate(now)
    value.observe_header(b"Retry-After: 100\r\n")
    saved = []
    class Store:
        async def extend(self, group, upstream, **kwargs):
            saved.append(kwargs["duration_sec"])
            if len(saved) == 1:
                now[0] = 20
                value.observe_header(b"Retry-After: 200\r\n")
                now[0] = 30
    try:
        await value.persist_cooldown(Store(), generation=1)
        assert saved == [100, 190]
    finally:
        value.close()
