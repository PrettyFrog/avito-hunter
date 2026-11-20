import httpx
import pytest

from hunter.transport.http_fetch import HttpFetchError, SlotHttpPool


@pytest.mark.parametrize("endpoint", [None, ""])
def test_new_slot_never_constructs_direct_client_without_proxy(endpoint, monkeypatch):
    def unexpected_client(**_kwargs):
        raise AssertionError("direct client must not be created")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    with pytest.raises(HttpFetchError, match="endpoint is required"):
        SlotHttpPool().get_fetcher("slot", endpoint)
