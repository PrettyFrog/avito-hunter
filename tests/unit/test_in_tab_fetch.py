from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
import pytest

from hunter.transport.in_tab_fetch import InTabSlotFetcher
from hunter.transport.http_fetch import HttpSlotFetcher, SlotHttpPool


@pytest.mark.asyncio
async def test_in_tab_fetch_success():
    mock_page = AsyncMock()
    mock_page.is_closed = lambda: False
    mock_page.evaluate = AsyncMock(return_value={
        "status": 200,
        "url": "https://www.avito.ru/moskva?q=iphone",
        "text": "<html><body>200 OK</body></html>",
        "error": None,
    })
    mock_factory = AsyncMock(return_value=mock_page)

    fetcher = InTabSlotFetcher(
        slot_id="slot-1",
        endpoint="direct",
        browser_factory=mock_factory,
    )
    resp = await fetcher.fetch("https://www.avito.ru/moskva?q=iphone")

    assert resp.status_code == 200
    assert resp.is_success is True
    assert "200 OK" in resp.text
    assert resp.error is None
    assert mock_page.evaluate.call_count == 1

    await fetcher.close()
    assert mock_page.close.call_count == 1


@pytest.mark.asyncio
async def test_in_tab_fetch_solves_pow_on_439():
    mock_page = AsyncMock()
    mock_page.is_closed = lambda: False
    mock_page.evaluate = AsyncMock(side_effect=[
        {"status": 439, "url": "https://www.avito.ru/firewall", "text": "firewallPow", "error": None},
        {"status": 200, "url": "https://www.avito.ru/moskva?q=iphone", "text": "OK after pow", "error": None},
    ])
    mock_factory = AsyncMock(return_value=mock_page)

    fetcher = InTabSlotFetcher(
        slot_id="slot-1",
        endpoint="direct",
        browser_factory=mock_factory,
    )

    with patch("hunter.transport.in_tab_fetch.solve_firewall_pow", new=AsyncMock(return_value=True)):
        resp = await fetcher.fetch("https://www.avito.ru/moskva?q=iphone")

    assert resp.status_code == 200
    assert "OK after pow" in resp.text
    assert mock_page.evaluate.call_count == 2
    await fetcher.close()


@pytest.mark.asyncio
async def test_in_tab_fetch_timeout():
    mock_page = AsyncMock()
    mock_page.is_closed = lambda: False
    mock_page.evaluate = AsyncMock(side_effect=TimeoutError())
    mock_factory = AsyncMock(return_value=mock_page)

    fetcher = InTabSlotFetcher(
        slot_id="slot-1",
        endpoint="direct",
        browser_factory=mock_factory,
        timeout_sec=0.1,
    )
    resp = await fetcher.fetch("https://www.avito.ru/moskva?q=iphone")
    assert resp.status_code == 0
    assert resp.error == "timeout"
    await fetcher.close()


def test_slot_http_pool_selects_in_tab_fetcher():
    mock_factory = AsyncMock()
    pool_browser = SlotHttpPool(browser_factory=mock_factory, use_browser_fetch=True)
    fetcher_direct = pool_browser.get_fetcher("slot-1", "direct")
    assert isinstance(fetcher_direct, InTabSlotFetcher)

    pool_raw = SlotHttpPool()
    fetcher_raw = pool_raw.get_fetcher("slot-2", "direct")
    assert isinstance(fetcher_raw, HttpSlotFetcher)


@pytest.mark.asyncio
async def test_in_tab_fetch_cookie_sync():
    mock_context = AsyncMock()
    mock_context.add_cookies = AsyncMock()
    mock_context.cookies = AsyncMock(return_value=[{"name": "sess", "value": "123"}])

    mock_page = AsyncMock()
    mock_page.is_closed = lambda: False
    mock_page.context = mock_context
    mock_factory = AsyncMock(return_value=mock_page)

    mock_session_store = AsyncMock()
    mock_session_payload = AsyncMock()
    mock_session_payload.cookies = [{"name": "cf_clearance", "value": "xyz"}]
    mock_session_store.load_session = AsyncMock(return_value=mock_session_payload)

    fetcher = InTabSlotFetcher(
        slot_id="slot-1",
        endpoint="direct",
        browser_factory=mock_factory,
        session_store=mock_session_store,
    )
    loaded = await fetcher.load_cookies_from_store()
    assert loaded == 1
    assert mock_context.add_cookies.call_count >= 1
    mock_context.add_cookies.assert_any_call([{"name": "cf_clearance", "value": "xyz"}])

    await fetcher.save_cookies_to_store(version=1, generation=1)
    mock_session_store.save_session.assert_called_once()
    await fetcher.close()
