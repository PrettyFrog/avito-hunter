import httpx
import pytest

from hunter.transport.http_fetch import HttpSlotFetcher


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination",
    [
        "http://127.0.0.1:8080/private",
        "https://evil.example/",
        "http://avito.ru/path",
        "https://user:secret@avito.ru/",
        "https://avito.ru:8443/",
    ],
)
async def test_redirect_is_checked_before_request(destination):
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"Location": destination})

    fetcher = HttpSlotFetcher("slot", transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.fetch("https://avito.ru/search")
        assert result.error == "unsafe_fetch_url"
        assert visited == ["https://avito.ru/search"]
        assert "secret" not in result.error
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_valid_same_service_redirect_is_followed():
    visited = []

    def handler(request):
        visited.append(str(request.url))
        if len(visited) == 1:
            return httpx.Response(302, headers={"Location": "https://www.avito.ru/new"})
        return httpx.Response(200, text="ok")

    fetcher = HttpSlotFetcher("slot", transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.fetch("https://avito.ru/search")
        assert result.text == "ok"
        assert len(visited) == 2
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_untrusted_initial_url_never_reaches_transport():
    def handler(_request):
        raise AssertionError("untrusted request must not reach transport")

    fetcher = HttpSlotFetcher("slot", transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.fetch("https://evil.example/search")
        assert result.error == "unsafe_fetch_url"
        assert result.url == ""
    finally:
        await fetcher.close()
