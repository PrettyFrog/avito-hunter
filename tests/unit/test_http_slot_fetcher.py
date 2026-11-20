"""Unit tests for HttpSlotFetcher and SlotHttpPool (S5 / T-479).

Covers:
- Isolated connection pools and cookies between slots.
- Successful HTTP fetch without starting Chromium / headless browser.
- Proper handling and classification of 403, 429, timeout, and redirect bounds.
- Body size limit enforcement (OOM prevention).
- Exact preservation of query URL and parameters.
- Hermetic test suite with mock transport (no live network).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from hunter.transport.http_fetch import (
    HttpSlotFetcher,
    SlotFetchConfig,
    SlotHttpPool,
)
from hunter.transport.session_store import SessionStore


@pytest.fixture
def session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


@pytest.mark.asyncio
async def test_different_slots_have_isolated_pools_and_cookies(session_store: SessionStore):
    # Setup cookies in session store for slot1 and slot2
    await session_store.save_session(
        slot_id="slot_01",
        endpoint="http://proxy1:8080",
        version=1,
        generation=1,
        cookies=[{"name": "sess_01", "value": "val_slot1", "domain": ".avito.ru"}],
    )
    await session_store.save_session(
        slot_id="slot_02",
        endpoint="http://proxy2:8080",
        version=1,
        generation=1,
        cookies=[{"name": "sess_02", "value": "val_slot2", "domain": ".avito.ru"}],
    )

    captured_cookies: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cookie_header = request.headers.get("cookie", "")
        if "sess_01" in cookie_header:
            captured_cookies["slot_01"] = cookie_header
        if "sess_02" in cookie_header:
            captured_cookies["slot_02"] = cookie_header
        return httpx.Response(200, text="<html>ok</html>")

    transport = httpx.MockTransport(handler)
    pool = SlotHttpPool(session_store=session_store)

    f1 = pool.get_fetcher("slot_01", endpoint="http://proxy1:8080", transport=transport)
    await f1.load_cookies_from_store()
    await f1.fetch("https://www.avito.ru/catalog")

    f2 = pool.get_fetcher("slot_02", endpoint="http://proxy2:8080", transport=transport)
    await f2.load_cookies_from_store()
    await f2.fetch("https://www.avito.ru/catalog")

    assert "val_slot1" in captured_cookies.get("slot_01", "")
    assert "val_slot2" not in captured_cookies.get("slot_01", "")
    assert "val_slot2" in captured_cookies.get("slot_02", "")
    assert "val_slot1" not in captured_cookies.get("slot_02", "")
    assert f1.client is not f2.client

    await pool.close_all()


@pytest.mark.asyncio
async def test_successful_http_does_not_spawn_chromium():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>catalog page</html>")

    transport = httpx.MockTransport(handler)
    fetcher = HttpSlotFetcher("slot_clean", transport=transport)

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        resp = await fetcher.fetch("https://www.avito.ru/all")
        assert resp.is_success is True
        assert resp.status_code == 200
        assert "catalog page" in resp.text
        assert mock_popen.call_count == 0
        assert mock_exec.call_count == 0

    await fetcher.close()


@pytest.mark.asyncio
async def test_403_and_429_classification_preserves_query():
    target_url = "https://www.avito.ru/all?q=sony+xm5&priceMax=35000&f=ASgCAg"

    def handler(request: httpx.Request) -> httpx.Response:
        if "blocked" in str(request.url):
            return httpx.Response(403, text="<html>Forbidden</html>")
        return httpx.Response(429, text="<html>Too Many Requests</html>")

    transport = httpx.MockTransport(handler)
    fetcher = HttpSlotFetcher("slot_err", transport=transport)

    # 403 test
    resp_403 = await fetcher.fetch(target_url + "&blocked=1")
    assert resp_403.is_blocked is True
    assert resp_403.status_code == 403
    assert resp_403.is_success is False
    assert "sony+xm5" in resp_403.url

    # 429 test
    resp_429 = await fetcher.fetch(target_url)
    assert resp_429.is_rate_limited is True
    assert resp_429.status_code == 429
    assert resp_429.is_success is False

    await fetcher.close()


@pytest.mark.asyncio
async def test_timeout_and_redirect_bounds():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Socket read timed out")

    fetcher_t = HttpSlotFetcher("slot_to", transport=httpx.MockTransport(timeout_handler))
    resp_to = await fetcher_t.fetch("https://www.avito.ru/slow")
    assert resp_to.status_code == 0
    assert resp_to.error == "timeout"
    await fetcher_t.close()

    # Redirect loop
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://www.avito.ru/loop"})

    cfg = SlotFetchConfig(max_redirects=3)
    fetcher_r = HttpSlotFetcher(
        "slot_redir", config=cfg, transport=httpx.MockTransport(redirect_handler)
    )
    resp_r = await fetcher_r.fetch("https://www.avito.ru/loop")
    assert resp_r.status_code == 0
    assert resp_r.error == "too_many_redirects"
    await fetcher_r.close()


@pytest.mark.asyncio
async def test_body_size_bound_exceeded():
    huge_body = b"A" * 2000

    def huge_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=huge_body)

    # Limit body size to 1000 bytes
    cfg = SlotFetchConfig(max_body_bytes=1000)
    fetcher = HttpSlotFetcher("slot_huge", config=cfg, transport=httpx.MockTransport(huge_handler))
    resp = await fetcher.fetch("https://www.avito.ru/huge")

    assert resp.status_code == 0
    assert "body_size_exceeded" in str(resp.error)
    assert resp.text == ""

    await fetcher.close()
