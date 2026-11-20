"""Scheduling deferral must retain its type through transport leaves."""
from types import SimpleNamespace

import pytest

from hunter.transport.http_fetch import HttpSlotFetcher
from hunter.transport.metered_http import HttpDeferred
from hunter.transport.session_warmup import perform_session_warmup


class DeferredClient:
    def __init__(self):
        self.calls = 0
        self.headers = {}
        self.error = HttpDeferred("cooldown", 9000)

    async def get(self, *args, **kwargs):
        self.calls += 1
        raise self.error


@pytest.mark.asyncio
async def test_warmup_preserves_deferred_identity_without_network_retry():
    client = DeferredClient()
    with pytest.raises(HttpDeferred) as caught:
        await perform_session_warmup(client, "https://www.avito.ru/")
    assert caught.value is client.error
    assert client.calls == 1


@pytest.mark.asyncio
async def test_native_fetch_preserves_deferred_identity_without_network_error_result():
    client = DeferredClient()
    fetcher = SimpleNamespace(curl_client=client, curl_client_b=None,
                             config=SimpleNamespace(timeout_sec=1, max_body_bytes=1024))
    with pytest.raises(HttpDeferred) as caught:
        await HttpSlotFetcher._fetch_curl(fetcher, "https://www.avito.ru/", {}, 0, "user_a")
    assert caught.value is client.error
    assert client.calls == 1
