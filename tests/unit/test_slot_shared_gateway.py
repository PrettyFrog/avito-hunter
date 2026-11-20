"""Two public slots using one egress group share one pre-wire admission state."""
import httpx
import pytest

from hunter.core.egress_config import EgressRegulatorConfig
from hunter.domain.request_budget import RequestBudget
from hunter.transport.http_fetch import SlotHttpPool
from hunter.transport.metered_http import HttpDeferred, RequestGateway


@pytest.mark.asyncio
async def test_slots_with_same_physical_group_share_httpx_admission():
    now = [10.0]
    wire = []
    async def respond(request):
        wire.append(str(request.url))
        return httpx.Response(200, text="ok")
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5,
                                            clock=lambda: now[0]))
    groups = EgressRegulatorConfig(groups={"a": "same-ip", "b": "same-ip"})
    pool = SlotHttpPool(gateway=gateway, egress_groups=groups)
    try:
        a = pool.get_fetcher("a", "http://proxy-a:8080", transport=httpx.MockTransport(respond))
        b = pool.get_fetcher("b", "http://proxy-b:8080", transport=httpx.MockTransport(respond))
        assert (await a.fetch("https://www.avito.ru/a")).status_code == 200
        with pytest.raises(HttpDeferred):
            await b.fetch("https://www.avito.ru/b")
        assert len(wire) == 1
        now[0] = 17.5
        assert (await b.fetch("https://www.avito.ru/b")).status_code == 200
        assert len(wire) == 2
    finally:
        await pool.close_all()
