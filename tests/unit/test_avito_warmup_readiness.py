from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from hunter.transport.catalog_api import warmup_client_session
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig
from hunter.transport.session_store import SessionStore


class FakeJar(dict):
    def set(self, name: str, value: str, domain: str = "", path: str = "") -> None:
        self[name] = value

    @property
    def jar(self):
        return [SimpleNamespace(name=k, value=v, domain=".avito.ru", path="/") for k, v in self.items()]


@pytest.mark.asyncio
async def test_embedded_redirect_is_not_ready() -> None:
    redirect_html = '<html><head><meta http-equiv="refresh" content="0;url=/blocked"></head></html>'
    client = SimpleNamespace(
        headers={"User-Agent": "test-UA"},
        cookies=FakeJar(),
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=redirect_html, cookies={})),
    )
    res = await warmup_client_session(client, "https://www.avito.ru/moskva")
    assert not res


@pytest.mark.asyncio
async def test_challenge_marker_in_200_is_not_ready() -> None:
    challenge_html = '<html><body><div data-marker="captcha-box">Solve captcha</div></body></html>'
    client = SimpleNamespace(
        headers={"User-Agent": "test-UA"},
        cookies=FakeJar(),
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=challenge_html, cookies={})),
    )
    res = await warmup_client_session(client, "https://www.avito.ru/moskva")
    assert not res


@pytest.mark.asyncio
async def test_empty_html_shell_is_not_ready() -> None:
    empty_html = "<html><body></body></html>"
    client = SimpleNamespace(
        headers={"User-Agent": "test-UA"},
        cookies=FakeJar(),
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=empty_html, cookies={})),
    )
    res = await warmup_client_session(client, "https://www.avito.ru/moskva")
    assert not res


@pytest.mark.asyncio
async def test_valid_catalog_html_is_ready() -> None:
    valid_html = '<html><body><div data-marker="catalog"><div data-marker="item">Item 1</div></div></body></html>'
    client = SimpleNamespace(
        headers={"User-Agent": "test-UA"},
        cookies=FakeJar({'ft': 'existing-token'}),
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=valid_html, cookies={})),
    )
    res = await warmup_client_session(client, "https://www.avito.ru/moskva")
    assert res


@pytest.mark.asyncio
async def test_failed_warmup_does_not_save_cookies_and_retries_on_next_poll(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path / "sessions")
    cfg = SlotFetchConfig(interleave_sessions=True)
    fetcher = HttpSlotFetcher("slot_warmup", endpoint="direct", config=cfg, session_store=store)
    try:
        # User B gets challenge and pow fails
        fetcher.curl_client_b.cookies = FakeJar({"cf_clearance": "challenge_token_only"})
        fetcher._session_index = 1  # Next fetch will target User B
        warmup_mock = AsyncMock(return_value=False)
        with patch("hunter.transport.catalog_api.warmup_client_session", warmup_mock):
            resp = await fetcher.fetch("https://www.avito.ru/web/1/js/items?categoryId=1")
            assert not resp.is_success
            assert not fetcher._user_b_ready
            # Verify cookies were NOT saved to store
            saved_b = await store.load_session("slot_warmup__user_b")
            assert saved_b is None

            # On the subsequent poll for B, warmup MUST be attempted again despite cookies in jar
            fetcher._session_index = 1
            await fetcher.fetch("https://www.avito.ru/web/1/js/items?categoryId=1")
            assert warmup_mock.call_count == 2
    finally:
        await fetcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_payload",
    [
        '{"status":"ok"}',
        '{"status":"ok","result":{"error":"upstream unavailable"}}',
        '{"items":"invalid"}',
        '<html><script>window.__initialData__={}</script></html>',
    ],
)
async def test_unconfirmed_catalog_structure_is_not_ready(invalid_payload: str) -> None:
    client = SimpleNamespace(
        headers={"User-Agent": "test-UA"},
        cookies=FakeJar(),
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=invalid_payload, cookies={})),
    )
    res = await warmup_client_session(client, "https://www.avito.ru/moskva")
    assert not res
