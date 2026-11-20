"""Close and cancellation must drain native ownership and cooldown persistence."""
import asyncio

import pytest
from curl_cffi import AsyncCurl, CurlOpt

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_curl import MeteredCurlSession
from hunter.transport.metered_http import RequestGateway
from tests.integration.test_metered_curl import server as _server, session

server = _server


@pytest.mark.asyncio
async def test_close_waits_for_active_native_release(server):
    url, _, finish = server
    budget = RequestBudget(enforce=True)
    client = session(RequestGateway(budget))
    try:
        await client.get(url + "/stream", stream=True)
        assert budget.snapshot("shared", "other")["active"] == 1
        await asyncio.wait_for(client.close(), 3)
        assert budget.snapshot("shared", "other")["active"] == 0
        assert not client._native_hooks
    finally:
        finish.set()
        await client.close()


@pytest.mark.asyncio
async def test_close_cancels_native_pool_waiters_without_new_request(server):
    url, wire, finish = server
    budget = RequestBudget()
    client = MeteredCurlSession(gateway=RequestGateway(budget), egress_group="shared", generation=1,
                               max_clients=1, emit=lambda event: None, curl_options={CurlOpt.NOPROXY: "*"})
    first = asyncio.create_task(client.get(url + "/headers", stream=True))
    second = None
    try:
        async with asyncio.timeout(3):
            while not wire:
                await asyncio.sleep(0)
        second = asyncio.create_task(client.get(url + "/ok"))
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.wait_for(client.close(), 3)
        assert first.done() and second.done()
        assert budget.snapshot("shared", "other")["active"] == 0
        assert wire == ["/headers"]
    finally:
        finish.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_repeated_cancel_drains_started_cooldown_write(server):
    url, _, _ = server
    started, release = asyncio.Event(), asyncio.Event()
    completed = []
    class Store:
        async def load(self, *args):
            return None
        async def extend(self, *args, **kwargs):
            started.set()
            await release.wait()
            completed.append(1)
    async with session(RequestGateway(RequestBudget(enforce=True), cooldown_store=Store())) as client:
        task = asyncio.create_task(client.get(url + "/first"))
        try:
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            assert not task.done(), "started persistence must drain before caller cancellation completes"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert completed == [1]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_closing_session_leaves_borrowed_async_curl_usable(server):
    url, _, finish = server
    shared = AsyncCurl()
    gateway = RequestGateway(RequestBudget())
    kwargs = dict(gateway=gateway, egress_group="shared", generation=1, async_curl=shared,
                  emit=lambda event: None, curl_options={CurlOpt.NOPROXY: "*"})
    a, b = MeteredCurlSession(**kwargs), MeteredCurlSession(**kwargs)
    try:
        await a.get(url + "/stream", stream=True)
        await asyncio.wait_for(a.close(), 3)
        assert (await b.get(url + "/ok")).text == "ok"
    finally:
        finish.set()
        await a.close()
        await b.close()
        await shared.close()


@pytest.mark.asyncio
async def test_close_cancels_preflight_waiter_before_native_allocation(server):
    url, wire, _ = server
    started = asyncio.Event()
    class Store:
        async def load(self, *args):
            started.set()
            await asyncio.Future()
    client = session(RequestGateway(RequestBudget(), cooldown_store=Store()))
    task = asyncio.create_task(client.get(url + "/ok"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(client.close(), 3)
        assert task.cancelled()
        assert not client._requests and not client._native_hooks
        assert wire == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
