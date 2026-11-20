"""HTTP response headers cannot hide later body failures from accounting."""
import asyncio

import httpx
import pytest

from hunter.transport.http_attempts import instrument_httpx_client


class Stream(httpx.AsyncByteStream):
    def __init__(self, error=None):
        self.error = error
        self.closed = False

    async def __aiter__(self):
        yield b"secret-body"
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [
    (None, "complete"), (httpx.ReadError("secret-error"), "error"),
    (asyncio.CancelledError(), "cancelled"),
])
async def test_body_outcome_correlates_without_leaking_payload(error, expected):
    events = []
    stream = Stream(error)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=stream))) as client:
        instrument_httpx_client(client, events.append)
        response = await client.send(client.build_request("GET", "https://example.test/?secret-query"), stream=True)
        assert not [e for e in events if e["event"] == "http_attempt_body_end"]
        if error is None:
            await response.aread()
        else:
            with pytest.raises(type(error)):
                await response.aread()
        await response.aclose()
    body = [e for e in events if e["event"] == "http_attempt_body_end"]
    assert len(body) == 1
    assert body[0]["outcome"] == expected
    start = next(e for e in events if e["event"] == "http_attempt_start")
    assert body[0]["attempt_id"] == start["attempt_id"]
    assert body[0]["parent_id"] == start["parent_id"]
    assert body[0]["duration_sec"] >= 0
    assert stream.closed
    assert "secret" not in str(events)


@pytest.mark.asyncio
async def test_unread_close_reports_closed_once_even_after_scope_exit():
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Stream()))) as client:
        instrument_httpx_client(client, events.append)
        with client.attempt_scope.use("recovery"):
            response = await client.send(client.build_request("GET", "https://example.test/"), stream=True)
        await response.aclose()
        await response.aclose()
    body = [e for e in events if e["event"] == "http_attempt_body_end"]
    assert len(body) == 1 and body[0]["outcome"] == "closed"
    assert body[0]["purpose"] == "recovery"


@pytest.mark.asyncio
async def test_body_observer_composes_with_gateway_without_double_count():
    from hunter.domain.request_budget import RequestBudget
    from hunter.transport.metered_http import MeteredTransport, RequestGateway
    events = []
    budget = RequestBudget(enforce=True)
    transport = MeteredTransport(httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Stream())), RequestGateway(budget),
        egress_group="shared", generation=1)
    async with httpx.AsyncClient(transport=transport) as client:
        instrument_httpx_client(client, events.append)
        async with client.stream("GET", "https://www.avito.ru/") as response:
            assert budget.snapshot("shared", "avito")["active"] == 1
            await response.aread()
    assert budget.snapshot("shared", "avito")["completed"] == 1
    assert sum(e["event"] == "http_attempt_start" for e in events) == 1
    assert sum(e["event"] == "http_attempt_end" for e in events) == 1
    assert sum(e["event"] == "http_attempt_body_end" for e in events) == 1
