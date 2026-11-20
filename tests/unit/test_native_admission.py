"""Native redirects consult admission before the next HTTP request."""
import pytest
from aiohttp import web
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import HttpDeferred


def admission(budget, deadline=100):
    from hunter.transport.native_admission import NativeHopAdmission
    permit = budget.before_attempt("shared", "avito", deadline=deadline).permit
    assert permit is not None
    return NativeHopAdmission(budget, permit, egress_group="shared", upstream="avito", deadline=deadline)


def test_retry_after_blocks_next_hop_and_releases_original_permit():
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gate = admission(budget)
    assert gate.before_request() == 0
    gate.observe_header(b"Retry-After: 9000\r\n")
    assert gate.before_request() == 1
    assert isinstance(gate.failure, HttpDeferred)
    assert gate.failure.not_before == 9010
    gate.close()
    gate.close()
    assert budget.snapshot("shared", "avito")["active"] == 0
    assert budget.snapshot("shared", "avito")["attempts"] == 1


def test_each_admitted_hop_counts_once_and_close_is_idempotent():
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gate = admission(budget)
    assert gate.before_request() == 0
    assert gate.before_request() == 0
    gate.close()
    gate.close()
    assert budget.snapshot("shared", "avito")["attempts"] == 2
    assert budget.snapshot("shared", "avito")["completed"] == 2


def test_deadline_expired_during_connect_does_not_send():
    now = [10]
    budget = RequestBudget(enforce=True, clock=lambda: now[0])
    gate = admission(budget, deadline=11)
    now[0] = 12
    assert gate.before_request() == 1
    assert gate.failure.reason == "expired"
    gate.close()
    assert budget.snapshot("shared", "avito")["active"] == 0


@pytest.mark.asyncio
async def test_real_native_redirect_cannot_bypass_header_cooldown():
    from hunter.transport.native_admission import NativePrerequisiteHook
    wire = []
    async def handle(request):
        wire.append(request.path)
        return web.Response(status=302, headers={"Location": "/next", "Retry-After": "9000"})
    app = web.Application()
    app.router.add_get("/{path}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gate = admission(budget)
    class Session(AsyncSession):
        async def pop_curl(self):
            curl = await super().pop_curl()
            self.hook = NativePrerequisiteHook(curl, gate)
            curl.setopt(CurlOpt.DEBUGFUNCTION, lambda kind, data: gate.observe_header(data) if kind == 1 else None)
            curl.setopt(CurlOpt.VERBOSE, 1)
            return curl
        def release_curl(self, curl):
            handle = getattr(curl, "_debug_handle", None)
            try:
                super().release_curl(curl)
            finally:
                gate.close()
                del handle
    try:
        async with Session(curl_options={CurlOpt.NOPROXY: "*"}) as session:
            with pytest.raises(RequestException) as caught:
                await session.get(f"http://127.0.0.1:{port}/first")
            assert int(caught.value.code) == 42
        assert wire == ["/first"]
        assert gate.failure.not_before == 9010
        assert budget.snapshot("shared", "avito")["active"] == 0
    finally:
        await runner.cleanup()


def test_cooldown_received_during_connect_blocks_even_first_hop():
    budget = RequestBudget(enforce=True, clock=lambda: 10, spacing_sec=30)
    gate = admission(budget)
    budget.restore_cooldown("shared", "avito", not_before=20)
    assert gate.before_request() == 1
    assert gate.failure.reason == "cooldown"
    assert gate.failure.not_before >= 20
    gate.close()
    assert budget.snapshot("shared", "avito")["active"] == 0


def test_callback_failure_aborts_instead_of_implicit_cffi_allow(monkeypatch):
    from curl_cffi import Curl, ffi
    from hunter.transport.native_admission import NativePrerequisiteHook
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gate = admission(budget)
    def fail():
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(gate, "before_request", fail)
    curl = Curl()
    try:
        hook = NativePrerequisiteHook(curl, gate)
        assert hook._callback(ffi.NULL, ffi.NULL, ffi.NULL, 0, 0) == 1
        assert isinstance(gate.failure, RuntimeError)
        assert budget.snapshot("shared", "avito")["active"] == 0
    finally:
        curl.close()
        gate.close()


def test_budget_shutdown_during_connect_prevents_first_http_request():
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gate = admission(budget)
    budget.close()
    assert gate.before_request() == 1
    assert gate.failure.reason == "closed"
    assert budget.snapshot("shared", "avito")["active"] == 0
