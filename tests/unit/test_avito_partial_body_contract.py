from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig


class TruncatedResponse:
    def __init__(self, chunks: list[bytes], exc: Exception | None = None, close_exc: Exception | None = None) -> None:
        self.status_code = 200
        self.url = "https://www.avito.ru/moskva"
        self.encoding = "utf-8"
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.astream_task = None
        self._chunks = chunks
        self._exc = exc
        self._close_exc = close_exc

    async def aiter_content(self):
        for c in self._chunks:
            yield c
        if self._exc:
            raise self._exc

    async def aclose(self) -> None:
        if self._close_exc:
            raise self._close_exc


@pytest.mark.asyncio
async def test_partial_body_with_catalog_returns_error() -> None:
    f = HttpSlotFetcher("audit", endpoint="direct", config=SlotFetchConfig(interleave_sessions=False))
    resp = TruncatedResponse([b'{"catalog":{"items":['], exc=httpx.ReadError("injected truncated body"))
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=resp)):
            r = await f.fetch("https://www.avito.ru/moskva")
        assert not r.is_success
        assert r.error is not None
        assert "truncated" in r.error.lower() or "readerror" in r.error.lower()
        assert r.status_code == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_partial_body_with_html_returns_error() -> None:
    f = HttpSlotFetcher("audit", endpoint="direct", config=SlotFetchConfig(interleave_sessions=False))
    resp = TruncatedResponse([b"<html></html>"], exc=httpx.RemoteProtocolError("connection closed prematurely"))
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=resp)):
            r = await f.fetch("https://www.avito.ru/moskva")
        assert not r.is_success
        assert r.error is not None
        assert r.status_code == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_aclose_error_does_not_mask_primary_read_error() -> None:
    f = HttpSlotFetcher("audit", endpoint="direct", config=SlotFetchConfig(interleave_sessions=False))
    resp = TruncatedResponse(
        [b"partial"],
        exc=httpx.ReadError("network broke"),
        close_exc=RuntimeError("aclose failed"),
    )
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=resp)):
            r = await f.fetch("https://www.avito.ru/moskva")
        assert not r.is_success
        assert r.error is not None
        assert "network broke" in r.error.lower() or "readerror" in r.error.lower()
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_child_stream_cancel_does_not_cancel_caller() -> None:
    child = asyncio.create_task(asyncio.sleep(1))
    await asyncio.sleep(0)

    class BrokenStream:
        astream_task = child
        async def aclose(self):
            await child

    from hunter.transport.http_body_reader import safe_teardown_response
    await safe_teardown_response(BrokenStream())
    assert child.done()


@pytest.mark.asyncio
async def test_caller_cancel_during_teardown_is_propagated() -> None:
    child = asyncio.create_task(asyncio.sleep(1))
    await asyncio.sleep(0)

    class BrokenStream:
        astream_task = child
        async def aclose(self):
            await child

    from hunter.transport.http_body_reader import safe_teardown_response
    async def caller():
        cur = asyncio.current_task()
        cur.cancel()
        await safe_teardown_response(BrokenStream())

    with pytest.raises(asyncio.CancelledError):
        await caller()

