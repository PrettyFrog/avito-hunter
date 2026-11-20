"""Shared native HTTP admission is verified by actual loopback wire requests."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from curl_cffi import CurlOpt

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import HttpDeferred, RequestGateway


@pytest.fixture
async def server():
    wire, finish = [], asyncio.Event()
    async def handle(request):
        wire.append(request.path)
        if request.path == "/headers":
            await finish.wait()
        if request.path == "/ok":
            return web.Response(text="ok")
        if request.path == "/redirect":
            return web.Response(status=302, headers={"Location": "/next", "Retry-After": "9000"})
        if request.path == "/stream":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"first")
            await finish.wait()
            return response
        return web.Response(status=429, headers={"Retry-After": "9000"})
    app = web.Application()
    app.router.add_get("/{path}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", wire, finish
    finally:
        finish.set()
        await runner.cleanup()


def session(gateway):
    from hunter.transport.metered_curl import MeteredCurlSession
    return MeteredCurlSession(gateway=gateway, egress_group="shared", generation=1,
                              emit=lambda event: None, curl_options={CurlOpt.NOPROXY: "*"})


@pytest.mark.asyncio
async def test_native_a_b_share_cooldown_and_preflight_makes_no_wire_request(server):
    url, wire, _ = server
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gateway = RequestGateway(budget)
    async with session(gateway) as a, session(gateway) as b:
        assert (await a.get(url + "/first")).status_code == 429
        b.pop_curl = AsyncMock(side_effect=AssertionError("Preflight must precede native allocation"))
        with pytest.raises(HttpDeferred):
            await b.get(url + "/second")
        b.pop_curl.assert_not_awaited()
    assert wire == ["/first"]
    assert budget.snapshot("shared", "other")["active"] == 0


@pytest.mark.asyncio
async def test_native_redirect_abort_is_typed_deferral(server):
    url, wire, _ = server
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    async with session(RequestGateway(budget)) as client:
        with pytest.raises(HttpDeferred) as caught:
            await client.get(url + "/redirect")
        assert caught.value.not_before == 9010
    assert wire == ["/redirect"]
    assert budget.snapshot("shared", "other")["active"] == 0


@pytest.mark.asyncio
async def test_stream_holds_native_permit_until_transfer_completion(server):
    url, wire, finish = server
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    async with session(RequestGateway(budget)) as client:
        response = await client.get(url + "/stream", stream=True)
        assert budget.snapshot("shared", "other")["active"] == 1
        finish.set()
        assert await response.acontent() == b"first"
        await response.aclose()
        await asyncio.sleep(0)
    assert wire == ["/stream"]
    assert budget.snapshot("shared", "other")["active"] == 0


@pytest.mark.asyncio
async def test_cancel_before_headers_releases_native_handle_for_reuse(server):
    url, wire, _ = server
    budget = RequestBudget(enforce=True)
    async with session(RequestGateway(budget)) as client:
        task = asyncio.create_task(client.get(url + "/headers", stream=True))
        async with asyncio.timeout(3):
            while not wire:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert budget.snapshot("shared", "other")["active"] == 0
        assert (await client.get(url + "/ok")).text == "ok"
    assert wire == ["/headers", "/ok"]


@pytest.mark.asyncio
async def test_aborted_redirect_persists_cooldown_before_raising(server):
    url, wire, _ = server
    class Store:
        def __init__(self):
            self.saved = []
        async def load(self, *args):
            return None
        async def extend(self, *args, **kwargs):
            self.saved.append(kwargs)
    store = Store()
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    async with session(RequestGateway(budget, cooldown_store=store)) as client:
        with pytest.raises(HttpDeferred):
            await client.get(url + "/redirect")
    assert store.saved == [{"generation": 1, "duration_sec": 9000}]
    assert wire == ["/redirect"]
