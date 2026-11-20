"""The HTTPX owner must gate its real transport route before any wire call."""
import httpx
import pytest

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import HttpDeferred, RequestGateway, meter_httpx_client


@pytest.mark.asyncio
async def test_httpx_client_admission_precedes_wire_and_keeps_one_route():
    now = [10.0]
    calls = []
    async def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="ok")
    budget = RequestBudget(enforce=True, spacing_sec=7.5, clock=lambda: now[0])
    gateway = RequestGateway(budget)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        meter_httpx_client(client, gateway, egress_group="shared", generation=1)
        assert (await client.get("https://www.avito.ru/one")).status_code == 200
        with pytest.raises(HttpDeferred):
            await client.get("https://www.avito.ru/two")
        assert len(calls) == 1
        now[0] = 17.5
        assert (await client.get("https://www.avito.ru/three")).status_code == 200
    assert len(calls) == 2
