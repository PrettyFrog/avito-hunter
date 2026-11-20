"""Wire-attempt accounting must retain errors and redact request details."""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_one_send_has_start_and_end_without_payload_or_headers():
    from hunter.transport.http_attempts import observe_attempt
    events = []
    calls = []
    ticks = iter([10.0, 10.5])
    async def send():
        calls.append(1)
        return SimpleNamespace(status_code=429, headers={"Set-Cookie": "secret"})
    response = await observe_attempt(send, events.append, purpose="poll", clock=lambda: next(ticks))
    assert response.status_code == 429
    assert calls == [1]
    assert [event["event"] for event in events] == ["http_attempt_start", "http_attempt_end"]
    assert events[0]["attempt_id"] == events[1]["attempt_id"]
    assert events[1]["status"] == 429 and events[1]["duration_sec"] == 0.5
    assert "secret" not in str(events) and "Set-Cookie" not in str(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("secret proxy credentials"), asyncio.CancelledError()])
async def test_failed_or_cancelled_send_is_recorded_and_propagated(error):
    from hunter.transport.http_attempts import observe_attempt
    events = []
    async def send():
        raise error
    with pytest.raises(type(error)):
        await observe_attempt(send, events.append, purpose="warmup")
    assert len(events) == 2
    assert events[1]["status"] is None
    assert events[1]["outcome"] == ("cancelled" if isinstance(error, asyncio.CancelledError) else "error")
    assert "secret proxy credentials" not in str(events)


@pytest.mark.asyncio
async def test_broken_event_sink_does_not_trigger_request_retry():
    from hunter.transport.http_attempts import observe_attempt
    calls = []
    async def send():
        calls.append(1)
        return SimpleNamespace(status_code=200)
    def broken_sink(event):
        raise OSError("secret log sink failure")
    response = await observe_attempt(send, broken_sink, purpose="poll")
    assert response.status_code == 200
    assert calls == [1]


@pytest.mark.asyncio
async def test_retry_after_is_reported_without_other_response_headers():
    from hunter.transport.http_attempts import observe_attempt
    events = []
    async def send():
        return SimpleNamespace(status_code=429, headers={"retry-after": "9000", "Set-Cookie": "secret"})
    await observe_attempt(send, events.append, purpose="poll")
    assert events[-1]["retry_after_sec"] == 9000
    assert "secret" not in str(events)


@pytest.mark.asyncio
async def test_httpx_redirects_each_emit_a_wire_attempt():
    import httpx
    from hunter.transport.http_attempts import instrument_httpx_client
    events, requests = [], []
    async def handler(request):
        requests.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/second"}) if len(requests) == 1 else httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        instrument_httpx_client(client, events.append)
        assert (await client.get("https://example.test/first?token=secret")).status_code == 200
    assert requests == ["/first", "/second"]
    assert [e["status"] for e in events if e["event"] == "http_attempt_end"] == [302, 200]
    assert "secret" not in str(events)


@pytest.mark.asyncio
async def test_httpx_observer_preserves_proxy_mounts_and_transport_ownership():
    import httpx
    from hunter.transport.http_attempts import instrument_httpx_client
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    client = httpx.AsyncClient(transport=transport, mounts={"https://mounted.test": transport})
    before = dict(client._mounts)
    instrument_httpx_client(client, lambda event: None)
    assert client._transport.inner is transport
    assert set(client._mounts) == set(before)
    assert all(client._mounts[key].inner is value for key, value in before.items() if value is not None)
    await client.aclose()


@pytest.mark.asyncio
async def test_slot_fetcher_httpx_emits_attempts_for_redirect_chain():
    import json
    from loguru import logger
    import httpx
    from hunter.transport.http_fetch import HttpSlotFetcher
    events = []
    sink = logger.add(lambda message: events.append(json.loads(message.record["message"].removeprefix("HTTP attempt "))),
                      filter=lambda record: record["message"].startswith("HTTP attempt "))
    wire = []
    def handler(request):
        wire.append(request)
        return httpx.Response(302, headers={"Location": "/moskva"}) if len(wire) == 1 else httpx.Response(200, text="ok")
    fetcher = HttpSlotFetcher("test", transport=httpx.MockTransport(handler))
    try:
        response = await fetcher.fetch("https://www.avito.ru/all")
    finally:
        await fetcher.close()
        logger.remove(sink)
    assert response.status_code == 200
    assert len(wire) == 2
    assert [event["status"] for event in events if event["event"] == "http_attempt_end"] == [302, 200]
