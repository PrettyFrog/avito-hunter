"""Paced native requests wait between real loopback sends, including redirects."""
import asyncio
import time

import pytest
from aiohttp import web
from curl_cffi import CurlOpt

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_curl import MeteredCurlSession
from hunter.transport.metered_http import HttpDeferred, RequestGateway


@pytest.fixture
async def paced_server():
    wire = []
    async def handle(request):
        wire.append((request.path, request.method, await request.read(),
                     dict(request.cookies), time.monotonic(), dict(request.headers)))
        if request.path == "/loop":
            return web.Response(status=302, headers={"Location": "/loop"})
        if request.path.startswith("/start/"):
            return web.Response(status=int(request.path.rsplit("/", 1)[1]),
                headers={"Location": "/final", "Set-Cookie": "progress=ready; Path=/"})
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", wire
    finally:
        await runner.cleanup()


def paced_client(gateway, **kwargs):
    return MeteredCurlSession(gateway=gateway, egress_group="shared", generation=1,
        emit=lambda event: None, curl_options={CurlOpt.NOPROXY: "*"}, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_paced_post_redirect_keeps_cookie_method_body_and_history(paced_server, status):
    url, wire = paced_server
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=0.03))
    async with paced_client(gateway) as client:
        response = await client.post(url + f"/start/{status}", json={"value": 1})
        assert response.text == "ok"
        assert len(response.history) == 1 and response.redirect_count == 1
    assert len(wire) == 2
    assert wire[1][4] - wire[0][4] >= 0.028  # 2ms loopback arrival jitter allowance.
    assert wire[1][3]["progress"] == "ready"
    assert wire[1][1] == ("GET" if status in (301, 302, 303) else "POST")
    assert wire[1][2] == (b"" if status in (301, 302, 303) else b'{"value":1}')
    assert gateway.budget.snapshot("shared", "other")["active"] == 0


@pytest.mark.asyncio
async def test_two_identities_share_interval_and_keep_independent_cookie_jars(paced_server):
    url, wire = paced_server
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=0.03))
    async with paced_client(gateway) as a, paced_client(gateway) as b:
        a.cookies.set("identity", "A", domain="127.0.0.1")
        b.cookies.set("identity", "B", domain="127.0.0.1")
        await asyncio.gather(a.get(url + "/final"), b.get(url + "/final"))
    assert wire[1][4] - wire[0][4] >= 0.028
    assert {item[3]["identity"] for item in wire} == {"A", "B"}


@pytest.mark.asyncio
async def test_deadline_before_next_hop_does_not_restart_chain(paced_server):
    url, wire = paced_server
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=1))
    async with paced_client(gateway, request_timeout_sec=0.1) as client:
        with pytest.raises(HttpDeferred):
            await client.get(url + "/start/302")
    assert [item[0] for item in wire] == ["/start/302"]
    assert gateway.waiting == 0
    assert gateway.budget.snapshot("shared", "other")["active"] == 0


@pytest.mark.asyncio
async def test_cancel_between_hops_does_not_send_or_leave_waiter(paced_server):
    url, wire = paced_server
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=1))
    async with paced_client(gateway) as client:
        task = asyncio.create_task(client.get(url + "/start/302", stream=True))
        async with asyncio.timeout(3):
            while not wire or gateway.waiting == 0:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(wire) == 1 and gateway.waiting == 0
    assert gateway.budget.snapshot("shared", "other")["active"] == 0


@pytest.mark.asyncio
async def test_redirect_limit_stops_chain_without_extra_send(paced_server):
    from curl_cffi.requests.exceptions import TooManyRedirects
    url, wire = paced_server
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=0.03))
    async with paced_client(gateway) as client:
        with pytest.raises(TooManyRedirects):
            await client.get(url + "/loop", max_redirects=1)
    assert len(wire) == 2
    assert gateway.budget.snapshot("shared", "other")["active"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_paced_final_stream_and_explicit_no_follow(paced_server, method):
    url, wire = paced_server
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=0.03))
    async with paced_client(gateway) as client:
        response = await client.request(method, url + "/start/303", stream=True)
        assert await response.acontent() == (b"ok" if method == "GET" else b"")
        await response.aclose()
        assert len(response.history) == 1
        response = await client.get(url + "/start/302", allow_redirects=False)
        assert response.status_code == 302 and response.history == []
    assert [item[0] for item in wire] == ["/start/303", "/final", "/start/302"]


@pytest.mark.asyncio
async def test_queued_request_keeps_origin_header_snapshot(paced_server):
    url, wire = paced_server
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=0.05))
    async with paced_client(gateway) as client:
        client.headers["User-Agent"] = "origin-profile"
        await client.get(url + "/final")
        task = asyncio.create_task(client.get(url + "/final"))
        async with asyncio.timeout(3):
            while gateway.waiting == 0:
                await asyncio.sleep(0)
        client.headers["User-Agent"] = "later-profile"
        await task
    assert wire[1][5]["User-Agent"] == "origin-profile"
