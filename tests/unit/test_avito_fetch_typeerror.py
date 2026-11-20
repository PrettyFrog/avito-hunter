import pytest

from hunter.transport.catalog_api import safe_fetch


class RaisingFetcher:
    def __init__(self) -> None:
        self.count = 0

    async def fetch(self, url: str, headers: dict[str, str] | None = None):
        self.count += 1
        if self.count == 1:
            raise TypeError("decoder implementation failed")
        return {"status": 200}


class LegacyFetcherWithoutHeaders:
    def __init__(self) -> None:
        self.called_url = ""

    async def fetch(self, url: str):
        self.called_url = url
        return {"status": 200, "url": url}


class ModernFetcherWithHeaders:
    def __init__(self) -> None:
        self.called_url = ""
        self.called_headers = None

    async def fetch(self, url: str, headers: dict[str, str] | None = None):
        self.called_url = url
        self.called_headers = headers
        return {"status": 200}


@pytest.mark.asyncio
async def test_internal_typeerror_is_raised_without_retry() -> None:
    fetcher = RaisingFetcher()
    with pytest.raises(TypeError, match="decoder implementation failed"):
        await safe_fetch(fetcher, "https://www.avito.ru/")
    assert fetcher.count == 1


@pytest.mark.asyncio
async def test_legacy_fetcher_without_headers_signature_works() -> None:
    fetcher = LegacyFetcherWithoutHeaders()
    res = await safe_fetch(fetcher, "https://www.avito.ru/catalog", headers={"Accept": "application/json"})
    assert res["status"] == 200
    assert fetcher.called_url == "https://www.avito.ru/catalog"


@pytest.mark.asyncio
async def test_modern_fetcher_receives_headers() -> None:
    fetcher = ModernFetcherWithHeaders()
    hdrs = {"Accept": "application/json"}
    res = await safe_fetch(fetcher, "https://www.avito.ru/catalog", headers=hdrs)
    assert res["status"] == 200
    assert fetcher.called_headers == hdrs
