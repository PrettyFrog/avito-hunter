"""Distinguish encoded body size from curl's decoded streaming chunks."""

import asyncio
import gzip

import pytest

from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig
from hunter.transport.response_metrics import declared_wire_body_bytes


@pytest.mark.parametrize("headers,expected", [
    ({}, None), ({"Content-Length": "123"}, 123),
    ({"content-length": "0"}, 0), ({"content-length": "-1"}, None),
    ({"content-length": "1, 2"}, None), ({"content-length": "١٢٣"}, None),
    ({"content-length": "9" * 5000}, None),
    ({"content-length": "5", "transfer-encoding": "chunked"}, None),
])
def test_declared_length_rejects_ambiguous_headers(headers, expected):
    assert declared_wire_body_bytes(headers) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_curl_reports_encoded_size_or_unknown(chunked):
    pytest.importorskip("curl_cffi", reason="Exercises the optional installed curl backend")
    body = ('{"title":"Объявление","price":12345}' * 200).encode()
    wire = gzip.compress(body)

    async def serve(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            framing = (b"Transfer-Encoding: chunked" if chunked
                       else f"Content-Length: {len(wire)}".encode())
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n" + framing
                         + b"\r\nConnection: close\r\n\r\n")
            writer.write((f"{len(wire):x}\r\n".encode() + wire + b"\r\n0\r\n\r\n")
                         if chunked else wire)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    fetcher = HttpSlotFetcher("wire", config=SlotFetchConfig(base_url=base))
    try:
        result = await asyncio.wait_for(fetcher.fetch("https://www.avito.ru/test"), 2)
        assert result.status_code == 200
        assert result.text.encode() == body
        assert result.wire_body_bytes == (None if chunked else len(wire))
    finally:
        await fetcher.close()
        server.close()
        await server.wait_closed()
