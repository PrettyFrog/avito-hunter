"""Confirmed routes survive access failures without hidden HTML rediscovery."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.transport.catalog_api import CatalogUrlResolver
from hunter.transport.http_fetch import HttpSlotFetcher


def feed():
    return json.dumps({"catalog": {"items": [{
        "id": 123456789, "title": "fixture", "urlPath": "/moskva/item_123456789", "price": 100,
    }]}})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 439, 200])
async def test_next_permitted_poll_keeps_json_route(monkeypatch, status):
    import hunter.application.public_poll_runtime as module
    resolver = CatalogUrlResolver(enabled=True, preserve_access_routes=True)
    query = "https://www.avito.ru/moskva?q=route-fixture"
    target = "https://www.avito.ru/web/1/js/items?categoryId=9"
    resolver._cache[query] = target
    for name in ("resolve_poll_target", "record_poll_response", "invalidate_poll_target", "invalidate_ambiguous_poll_target"):
        method = {"invalidate_poll_target": "invalidate", "invalidate_ambiguous_poll_target": "invalidate_ambiguous"}.get(name, name)
        monkeypatch.setattr(module, name, getattr(resolver, method))
    requests = []
    async def handler(request):
        requests.append(str(request.url))
        return httpx.Response(status if len(requests) == 1 else 200,
                              text='{"error":"denied"}' if len(requests) == 1 else feed())
    fetcher = HttpSlotFetcher("route", transport=httpx.MockTransport(handler))
    ingest, signals = AsyncMock(), AsyncMock()
    runtime = PublicPollRuntime(None, SimpleNamespace(get_fetcher=lambda *args: fetcher),
                                ingest, PollPolicy(), slot_signal_handler=signals)
    assignment = QueryAssignment("route", "direct", "q", query, 1, 0)
    try:
        await runtime.poll(assignment, 1)
        assert requests == [target] and ingest.await_count == 0
        # The scheduler/supervisor owns the cooldown; this represents its next permitted poll.
        await runtime.poll(assignment, 2)
        assert requests == [target, target]
        assert ingest.await_count == 1
    finally:
        await fetcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize("retry_after", [None, "9000"])
async def test_obsolete_route_has_one_bounded_html_fallback(monkeypatch, status, retry_after):
    import hunter.application.public_poll_runtime as module
    resolver = CatalogUrlResolver(enabled=True, preserve_access_routes=True)
    query = "https://www.avito.ru/moskva?q=obsolete-route"
    target = "https://www.avito.ru/web/1/js/items?categoryId=9"
    resolver._cache[query] = target
    for name in ("resolve_poll_target", "record_poll_response", "invalidate_poll_target", "invalidate_ambiguous_poll_target"):
        method = {"invalidate_poll_target": "invalidate", "invalidate_ambiguous_poll_target": "invalidate_ambiguous"}.get(name, name)
        monkeypatch.setattr(module, name, getattr(resolver, method))
    requests = []
    async def handler(request):
        requests.append(str(request.url))
        headers = {"Retry-After": retry_after} if retry_after else {}
        return httpx.Response(status if len(requests) == 1 else 200, headers=headers,
                              text="gone" if len(requests) == 1 else feed())
    fetcher = HttpSlotFetcher("route", transport=httpx.MockTransport(handler))
    ingest = AsyncMock()
    runtime = PublicPollRuntime(None, SimpleNamespace(get_fetcher=lambda *args: fetcher),
                                ingest, PollPolicy())
    try:
        await runtime.poll(QueryAssignment("route", "direct", "q", query, 1, 0), 1)
        assert requests == ([target] if retry_after else [target, query])
        assert ingest.await_count == (0 if retry_after else 1)
        assert resolver.resolve_poll_target(query)[0] == query
    finally:
        await fetcher.close()
