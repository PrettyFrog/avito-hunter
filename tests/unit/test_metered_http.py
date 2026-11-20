"""Gate failures, deferrals, redirects and cancellation use a fake wire counter."""
import asyncio

import httpx
import pytest

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import HttpDeferred, MeteredTransport, RequestGateway
from hunter.storage.request_cooldown import CooldownRecord


@pytest.mark.asyncio
async def test_shared_ip_restores_other_host_cooldown_before_avito_wire():
    calls = []
    class Store:
        wall_clock = staticmethod(lambda: 1000.0)
        async def load(self, group, upstream):
            calls.append((group, upstream))
            if upstream == "other":
                return CooldownRecord(1, 1100.0, 1000.0, 100.0)
            return None
    budget = RequestBudget(enforce=True, shared_upstreams=True, clock=lambda: 10)
    gateway = RequestGateway(budget, cooldown_store=Store())
    with pytest.raises(HttpDeferred):
        await gateway.acquire(egress_group="ip", upstream="avito", deadline=200)
    assert calls == [("ip", "avito"), ("ip", "other")]


@pytest.mark.asyncio
async def test_shared_identity_cooldown_prevents_second_http_send():
    now, wire = [10.0], []
    budget = RequestBudget(enforce=True, clock=lambda: now[0])
    gateway = RequestGateway(budget)
    async def send():
        wire.append(1)
        return httpx.Response(429, headers={"Retry-After": "9000"})
    args = dict(egress_group="same-ip", upstream="avito", generation=1, deadline=10000)
    assert (await gateway.run(send, **args)).status_code == 429
    for _ in range(10):
        with pytest.raises(HttpDeferred) as caught:
            await gateway.run(send, **args)
        assert caught.value.not_before == 9010
    assert wire == [1]
    assert budget.snapshot("same-ip", "avito")["active"] == 0


@pytest.mark.asyncio
async def test_redirect_hop_cannot_ignore_retry_after():
    wire = []
    async def handler(request):
        wire.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/next", "Retry-After": "9000"})
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    transport = MeteredTransport(httpx.MockTransport(handler), RequestGateway(budget),
                                egress_group="ip", generation=1)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(HttpDeferred):
            await client.get("https://www.avito.ru/first")
    assert wire == ["/first"]
    assert budget.snapshot("ip", "avito")["attempts"] == 1


@pytest.mark.asyncio
async def test_cancel_and_error_release_permission_without_closing_borrowed_client():
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gateway = RequestGateway(budget)
    started = asyncio.Event()
    async def hang():
        started.set()
        await asyncio.Future()
    args = dict(egress_group="ip", upstream="avito", generation=1, deadline=20)
    task = asyncio.create_task(gateway.run(hang, **args))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async def fail():
        raise OSError("synthetic failure")
    with pytest.raises(OSError):
        await gateway.run(fail, **args)
    assert budget.snapshot("ip", "avito")["active"] == 0
    assert budget.snapshot("ip", "avito")["completed"] == 2


@pytest.mark.asyncio
async def test_persistence_failure_is_fail_closed_before_wire():
    class BrokenStore:
        async def load(self, *args):
            raise OSError("disk unavailable")
    gateway = RequestGateway(RequestBudget(enforce=True), cooldown_store=BrokenStore())
    wire = []
    async def send():
        wire.append(1)
    with pytest.raises(OSError):
        await gateway.run(send, egress_group="ip", upstream="avito", generation=1,
                          deadline=float("inf"))
    assert wire == []


@pytest.mark.asyncio
async def test_expired_request_does_not_send_or_queue():
    gateway = RequestGateway(RequestBudget(clock=lambda: 10))
    wire = []
    async def send():
        wire.append(1)
    with pytest.raises(HttpDeferred, match="expired"):
        await gateway.run(send, egress_group=None, upstream="avito", generation=1, deadline=10)
    assert wire == []
