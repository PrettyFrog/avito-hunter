"""Admission covers the response body, not merely receipt of headers."""
import asyncio

import httpx
import pytest

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import HttpDeferred, RequestGateway


class Body(httpx.AsyncByteStream):
    def __init__(self, error=None):
        self.error = error
        self.closed = False

    async def __aiter__(self):
        yield b"first"
        if self.error:
            raise self.error
        yield b"last"

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_holds_shared_admission_until_closed():
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gateway = RequestGateway(budget)
    body = Body()
    args = dict(egress_group="shared", upstream="avito", generation=1, deadline=20)

    async def send():
        return httpx.Response(200, stream=body)

    response = await gateway.run(send, **args)
    assert budget.snapshot("shared", "avito")["active"] == 1
    with pytest.raises(HttpDeferred, match="busy"):
        await gateway.run(send, **args)
    await response.aclose()
    await response.aclose()
    assert body.closed
    assert budget.snapshot("shared", "avito")["active"] == 0
    assert budget.snapshot("shared", "avito")["completed"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, httpx.ReadError("synthetic"), asyncio.CancelledError()])
async def test_body_completion_error_and_cancel_release_once(error):
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    gateway = RequestGateway(budget)
    body = Body(error)

    async def send():
        return httpx.Response(200, stream=body)

    response = await gateway.run(send, egress_group="shared", upstream="avito", generation=1, deadline=20)
    assert budget.snapshot("shared", "avito")["active"] == 1
    if error is None:
        assert await response.aread() == b"firstlast"
    else:
        with pytest.raises(type(error)):
            await response.aread()
    await response.aclose()
    assert body.closed
    assert budget.snapshot("shared", "avito")["active"] == 0
    assert budget.snapshot("shared", "avito")["completed"] == 1
