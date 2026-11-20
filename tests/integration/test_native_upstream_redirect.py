"""All traffic is loopback: avito.ru is resolved to 127.0.0.1 inside network namespace."""
import pytest
from aiohttp import web
from curl_cffi import CurlOpt

from hunter.domain.request_budget import RequestBudget
from hunter.storage.request_cooldown import CooldownRecord
from hunter.transport.metered_curl import MeteredCurlSession
from hunter.transport.metered_http import HttpDeferred, RequestGateway


@pytest.fixture
async def target():
    wire = []
    async def handler(request):
        wire.append(request.path)
        if request.path.startswith("/redirect"):
            headers = {"Location": f"http://127.0.0.1:{port}/next"}
            if request.path == "/redirect-ra":
                headers["Retry-After"] = "9000"
            return web.Response(status=302, headers=headers)
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/{path}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://avito.ru:{port}", port, wire
    finally:
        await runner.cleanup()


def client(gateway, port):
    return MeteredCurlSession(gateway=gateway, egress_group="shared", generation=1,
        emit=lambda event: None,
        curl_options={CurlOpt.NOPROXY: "*", CurlOpt.RESOLVE: [f"avito.ru:{port}:127.0.0.1"]})


@pytest.mark.asyncio
async def test_redirect_must_check_new_upstream_cooldown(target):
    url, port, wire = target
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    budget.restore_cooldown("shared", "other", not_before=9010)
    async with client(RequestGateway(budget), port) as session:
        with pytest.raises(HttpDeferred):
            await session.get(url + "/redirect")
    assert wire == ["/redirect"]
    assert budget.snapshot("shared", "avito")["active"] == 0
    assert budget.snapshot("shared", "other")["attempts"] == 0


@pytest.mark.asyncio
async def test_redirect_uses_separate_attempt_and_retry_after_scope(target):
    url, port, wire = target
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    async with client(RequestGateway(budget), port) as session:
        assert (await session.get(url + "/redirect-ra")).text == "ok"
    assert wire == ["/redirect-ra", "/next"]
    assert budget.snapshot("shared", "avito")["attempts"] == 1
    assert budget.snapshot("shared", "other")["attempts"] == 1
    assert budget.cooldown_deadline("shared", "avito") == 9010
    assert budget.cooldown_deadline("shared", "other") == 0


@pytest.mark.asyncio
async def test_redirect_loads_persisted_target_floor_before_connect(target):
    url, port, wire = target
    class Store:
        wall_clock = staticmethod(lambda: 1000)
        async def load(self, group, upstream):
            return CooldownRecord(1, 10000, 1000, 9000) if upstream == "other" else None
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    async with client(RequestGateway(budget, cooldown_store=Store()), port) as session:
        with pytest.raises(HttpDeferred):
            await session.get(url + "/redirect")
    assert wire == ["/redirect"]
    assert budget.cooldown_deadline("shared", "other") == 9010
