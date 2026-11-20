"""Operation labels must survive native callbacks without task-context mixing."""
import asyncio

import httpx
import pytest


@pytest.mark.asyncio
async def test_httpx_concurrent_scopes_keep_purpose_and_parent():
    from hunter.transport.attempt_scope import client_scopes
    from hunter.transport.http_attempts import instrument_httpx_client
    events = []
    async def handler(request):
        await asyncio.sleep(0)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        instrument_httpx_client(client, events.append)
        async def run(purpose):
            with client_scopes([client], purpose):
                await client.get("https://example.test/")
        await asyncio.gather(run("warmup"), run("retry"))
    starts = [e for e in events if e["event"] == "http_attempt_start"]
    assert {e["purpose"] for e in starts} == {"warmup", "retry"}
    assert len({e["parent_id"] for e in starts}) == 2
    for start in starts:
        end = next(e for e in events if e["event"] == "http_attempt_end" and e["attempt_id"] == start["attempt_id"])
        assert (end["purpose"], end["parent_id"]) == (start["purpose"], start["parent_id"])


def test_callback_capture_keeps_labels_after_scope_exits():
    from hunter.transport.attempt_scope import AttemptScope
    events = []
    scope = AttemptScope()
    with scope.use("warmup"):
        emit = scope.capture_emitter(events.append)
    emit({"event": "end", "purpose": "poll"})
    assert events[0]["purpose"] == "warmup"
    assert events[0]["parent_id"]


@pytest.mark.asyncio
async def test_retry_and_warmup_helpers_label_actual_httpx_requests():
    from types import SimpleNamespace
    from hunter.application.poll_attempts import safe_fetch_attempt
    from hunter.transport.attempt_scope import scoped_http_call
    from hunter.transport.http_attempts import instrument_httpx_client
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
        instrument_httpx_client(client, events.append)
        async def fetch(url, headers=None):
            return await client.get(url, headers=headers)
        fetcher = SimpleNamespace(client=client, fetch=fetch)
        await safe_fetch_attempt(fetcher, "https://example.test/", purpose="retry")
        await scoped_http_call(client, "warmup", "get", "https://example.test/")
    assert [event["purpose"] for event in events if event["event"] == "http_attempt_start"] == ["retry", "warmup"]
