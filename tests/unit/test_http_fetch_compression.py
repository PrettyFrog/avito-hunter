"""Negotiate modern compression without changing decoded search coverage."""

import importlib

import httpx
import pytest

from hunter.transport.http_fetch import HttpSlotFetcher


@pytest.mark.asyncio
async def test_slot_advertises_brotli_and_zstandard() -> None:
    fetcher = HttpSlotFetcher("compression")
    try:
        advertised = set(fetcher.client.headers["accept-encoding"].split(", "))
        assert {"gzip", "deflate", "br", "zstd"} <= advertised
    finally:
        await fetcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding,module", [("br", "brotli"), ("zstd", "zstandard")])
async def test_compressed_stream_preserves_body_and_wire_metric(encoding, module) -> None:
    codec = importlib.import_module(module)
    body = ('{"title":"Объявление","price":12345}' * 100).encode()
    wire = (codec.compress(body) if encoding == "br"
            else codec.ZstdCompressor().compress(body))

    class WireStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(wire), 7):
                yield wire[offset:offset + 7]

    def respond(request):
        assert encoding in request.headers["accept-encoding"].split(", ")
        return httpx.Response(200, stream=WireStream(), headers={
            "content-encoding": encoding, "content-type": "application/json; charset=utf-8",
        })

    fetcher = HttpSlotFetcher("compression", transport=httpx.MockTransport(respond))
    try:
        result = await fetcher.fetch("https://www.avito.ru/web/1/js/items")
        assert result.status_code == 200
        assert result.text.encode() == body
        assert result.wire_body_bytes == len(wire)
    finally:
        await fetcher.close()
