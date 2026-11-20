import httpx
import pytest

from hunter.transport.http_fetch import HttpFetchError, HttpSlotFetcher


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:secret-value@proxy:8080",
        "http://proxy:8080/?token=secret-value",
        "http://proxy:8080/secret-value",
    ],
)
def test_legacy_secret_url_rejected_before_client_creation(endpoint, monkeypatch):
    def unexpected_client(**_kwargs):
        raise AssertionError("unsafe endpoint must not create a client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    with pytest.raises(HttpFetchError) as error:
        HttpSlotFetcher("slot", endpoint=endpoint)
    assert "secret-value" not in str(error.value)
