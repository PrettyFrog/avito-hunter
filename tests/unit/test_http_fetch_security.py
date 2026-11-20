"""Proxy and session identity must be validated before any HTTP request."""

from pathlib import Path

import httpx
import pytest

from hunter.transport.http_fetch import HttpFetchError, HttpSlotFetcher, SlotHttpPool
from hunter.transport.session_store import EndpointMismatchError, SessionStore


def test_unsupported_proxy_cannot_fall_back_to_direct_route() -> None:
    with pytest.raises(HttpFetchError):
        HttpSlotFetcher("slot", endpoint="socks5://user:password@proxy:1080",
                        transport=httpx.MockTransport(lambda request: httpx.Response(200)))


def test_pool_rejects_endpoint_switch_for_existing_slot() -> None:
    pool = SlotHttpPool()
    pool.get_fetcher("slot", endpoint="http://proxy-one:8080",
                     transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(HttpFetchError):
        pool.get_fetcher("slot", endpoint="http://proxy-two:8080")
    with pytest.raises(HttpFetchError):
        pool.get_fetcher("slot", endpoint=None)


@pytest.mark.asyncio
async def test_direct_fetcher_rejects_proxy_bound_cookies(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    await store.save_session("slot", "http://user:password@proxy:8080", 1, 1,
                             cookies=[{"name": "auth", "value": "secret", "domain": ".avito.ru"}])
    fetcher = HttpSlotFetcher("slot", session_store=store,
                              transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        with pytest.raises(EndpointMismatchError) as error:
            await fetcher.load_cookies_from_store()
        assert "password" not in str(error.value)
    finally:
        await fetcher.close()
