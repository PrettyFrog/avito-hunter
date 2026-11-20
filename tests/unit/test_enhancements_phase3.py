from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from hunter.transport.http_fetch import (
    HttpSlotFetcher,
    SlotFetchConfig,
    SlotFetchResponse,
)


@pytest.mark.asyncio
async def test_multi_session_interleaving_alternates_clients():
    cfg = SlotFetchConfig(interleave_sessions=True)
    fetcher = HttpSlotFetcher("slot-1", endpoint="direct", config=cfg)

    # Setup two mock curl sessions
    mock_session_a = AsyncMock()
    mock_session_b = AsyncMock()

    class FakeResp:
        status_code = 200
        url = "https://www.avito.ru/catalog"
        headers = {}
        cookies = {}
        encoding = "utf-8"
        astream_task = None

        async def aiter_content(self):
            yield b"<!DOCTYPE html><html><body>catalog</body></html>"

        async def aclose(self):
            pass

    mock_session_a.get = AsyncMock(return_value=FakeResp())
    mock_session_b.get = AsyncMock(return_value=FakeResp())

    fetcher.curl_client = mock_session_a
    fetcher.curl_client_b = mock_session_b

    # Poll 1 -> Session A
    await fetcher.fetch("https://www.avito.ru/catalog")
    assert mock_session_a.get.call_count == 1
    assert mock_session_b.get.call_count == 0

    # Poll 2 -> Session B
    await fetcher.fetch("https://www.avito.ru/catalog")
    assert mock_session_a.get.call_count == 1
    assert mock_session_b.get.call_count == 1

    # Poll 3 -> Session A
    await fetcher.fetch("https://www.avito.ru/catalog")
    assert mock_session_a.get.call_count == 2
    assert mock_session_b.get.call_count == 1

    # Poll 4 -> Session B
    await fetcher.fetch("https://www.avito.ru/catalog")
    assert mock_session_a.get.call_count == 2
    assert mock_session_b.get.call_count == 2


@pytest.mark.asyncio
async def test_close_closes_both_curl_sessions():
    cfg = SlotFetchConfig(interleave_sessions=True)
    fetcher = HttpSlotFetcher("slot-1", endpoint="direct", config=cfg)

    mock_a = AsyncMock()
    mock_b = AsyncMock()
    fetcher.curl_client = mock_a
    fetcher.curl_client_b = mock_b

    await fetcher.close()
    assert mock_a.close.call_count == 1
    assert mock_b.close.call_count == 1


@pytest.mark.asyncio
async def test_session_b_warms_up_with_independent_cookies():
    cfg = SlotFetchConfig(interleave_sessions=True)
    fetcher = HttpSlotFetcher("slot-1", endpoint="direct", config=cfg)

    mock_session_a = AsyncMock()
    mock_session_b = AsyncMock()

    class FakeResp:
        status_code = 200
        url = "https://www.avito.ru/web/1/js/items"
        headers = {}
        cookies = {}
        encoding = "utf-8"
        astream_task = None

        async def aiter_content(self):
            yield b'{"catalog": {"items": []}}'

        async def aclose(self):
            pass

    mock_session_a.get = AsyncMock(return_value=FakeResp())
    mock_session_a.cookies = {"sess": "A"}

    mock_session_b.get = AsyncMock(return_value=FakeResp())
    mock_session_b.cookies = {"ft": "token_b"}

    fetcher.curl_client = mock_session_a
    fetcher.curl_client_b = mock_session_b

    # Poll 1 -> Session A (XHR)
    await fetcher.fetch("https://www.avito.ru/web/1/js/items")
    assert mock_session_a.get.call_count == 1
    assert mock_session_a.cookies == {"sess": "A"}

    # Poll 2 -> Session B (XHR with empty cookies triggers warmup then XHR)
    await fetcher.fetch("https://www.avito.ru/web/1/js/items", headers={"Referer": "https://www.avito.ru/catalog"})
    # Session B got called twice (warmup + actual items fetch)
    assert mock_session_b.get.call_count == 2
    # Verify Session A and Session B cookies remain independent
    assert mock_session_a.cookies != mock_session_b.cookies


@pytest.mark.asyncio
async def test_store_load_preserves_independent_sessions_and_user_agents(tmp_path):
    from types import SimpleNamespace

    from hunter.transport.session_store import SessionStore

    class SpyStore(SessionStore):
        def __init__(self, directory):
            super().__init__(directory)
            self.saved = {}

        async def save_session(self, slot_id, **kw):
            self.saved[slot_id] = kw
            return await super().save_session(slot_id=slot_id, **kw)

    from hunter.transport.http_fetch import DEFAULT_FIREFOX_USER_AGENT, DEFAULT_CHROME_USER_AGENT
    store = SpyStore(tmp_path / "sessions")
    for sid, token, ua in (("slot-1", "A_token", DEFAULT_FIREFOX_USER_AGENT),
                            ("slot-1__user_b", "B_token", DEFAULT_CHROME_USER_AGENT)):
        await store.save_session(sid, endpoint="direct", version=1, generation=1,
                                 cookies=[{"name": "u", "value": token, "domain": ".avito.ru"}],
                                 storage_state={"user_agent": ua})
    store.saved.clear()
    fetcher = HttpSlotFetcher("slot-1", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True), session_store=store)
    try:
        await fetcher.load_cookies_from_store()

        assert fetcher.curl_client.cookies.get("u") == "A_token"
        assert fetcher.curl_client_b.cookies.get("u") == "B_token"
        assert fetcher.curl_client.headers.get("User-Agent") == DEFAULT_FIREFOX_USER_AGENT
        assert fetcher.curl_client_b.headers.get("User-Agent") == DEFAULT_CHROME_USER_AGENT

        await fetcher.save_cookies_to_store(1, 1)
        assert "slot-1" in store.saved
        assert "slot-1__user_b" in store.saved
        assert any(c["name"] == "u" and c["value"] == "A_token" for c in store.saved["slot-1"]["cookies"])
        assert any(c["name"] == "u" and c["value"] == "B_token" for c in store.saved["slot-1__user_b"]["cookies"])
        assert (await store.load_session("slot-1")).metadata.generation == 2
        assert (await store.load_session("slot-1__user_b")).metadata.generation == 2
    finally:
        await fetcher.close()
