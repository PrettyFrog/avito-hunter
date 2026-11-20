"""Price-watch HTTP must share the public egress admission owner."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from hunter.application.price_wiring import wire_price_watch
from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import RequestGateway


@pytest.mark.asyncio
async def test_price_fetch_uses_shared_egress_budget_before_wire():
    now = [10.0]
    wire = []
    async def respond(request):
        wire.append(str(request.url))
        return httpx.Response(200, text="synthetic card")
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5,
                                            clock=lambda: now[0]))
    owner = SimpleNamespace(writer=Mock(), settings=SimpleNamespace(
        public_v2=SimpleNamespace(price_watch_enabled=True)), admission_ctrl=Mock(),
        poll_sequence=None, egress_gateway=gateway)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    fetcher, _ = wire_price_watch(owner, client=client)
    try:
        await fetcher.fetch_card_price("https://www.avito.ru/a")
        deferred = await fetcher.fetch_card_price("https://www.avito.ru/b")
        assert deferred.status == "deferred"
        assert len(wire) == 1
    finally:
        await fetcher.close()
