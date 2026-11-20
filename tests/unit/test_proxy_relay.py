from __future__ import annotations

import asyncio
import base64
from urllib.parse import urlsplit

import pytest

from hunter.transport.proxy_credentials import ProxyCredentials
from hunter.transport.proxy_relay import LocalProxyRelay, ProxyRelayError

CREDENTIALS = ProxyCredentials("operator", "secret")
EXPECTED_AUTH = "Basic " + base64.b64encode(b"operator:secret").decode("ascii")


class FakeUpstreamProxy:
    """Minimal HTTP proxy that requires the relay-injected Basic header."""

    def __init__(self, accept_auth: bool = True) -> None:
        self.accept_auth = accept_auth
        self.requests: list[bytes] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(head)
            authorized = self.accept_auth and EXPECTED_AUTH.encode() in head
            if not authorized:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                await writer.drain()
                return
            if head.startswith(b"CONNECT"):
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                writer.write(await reader.read(1024))
                await writer.drain()
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        finally:
            writer.close()


async def _relay(upstream_port: int) -> LocalProxyRelay:
    relay = LocalProxyRelay(f"http://127.0.0.1:{upstream_port}", CREDENTIALS)
    await relay.start()
    return relay


@pytest.mark.asyncio
async def test_relay_injects_basic_auth_on_connect():
    upstream = FakeUpstreamProxy()
    port = await upstream.start()
    relay = await _relay(port)
    try:
        target = urlsplit(relay.url)
        reader, writer = await asyncio.open_connection(target.hostname, target.port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
        assert b"200" in response
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4), timeout=2) == b"ping"
        writer.close()
    finally:
        await relay.stop()
        await upstream.stop()

    assert len(upstream.requests) == 1
    assert upstream.requests[0].startswith(b"CONNECT example.com:443")
    assert EXPECTED_AUTH.encode() in upstream.requests[0]


@pytest.mark.asyncio
async def test_relay_forwards_plain_http_with_auth():
    upstream = FakeUpstreamProxy()
    port = await upstream.start()
    relay = await _relay(port)
    try:
        target = urlsplit(relay.url)
        reader, writer = await asyncio.open_connection(target.hostname, target.port)
        writer.write(
            b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=2)
        writer.close()
        assert b"200 OK" in data
        assert data.endswith(b"ok")
    finally:
        await relay.stop()
        await upstream.stop()

    assert EXPECTED_AUTH.encode() in upstream.requests[0]


@pytest.mark.asyncio
async def test_relay_returns_502_when_upstream_rejects_auth():
    upstream = FakeUpstreamProxy(accept_auth=False)
    port = await upstream.start()
    relay = await _relay(port)
    try:
        target = urlsplit(relay.url)
        reader, writer = await asyncio.open_connection(target.hostname, target.port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
        writer.close()
        assert b"502" in response
    finally:
        await relay.stop()
        await upstream.stop()


@pytest.mark.asyncio
async def test_relay_stop_closes_listener():
    upstream = FakeUpstreamProxy()
    port = await upstream.start()
    relay = await _relay(port)
    url = relay.url
    await relay.stop()
    await upstream.stop()

    target = urlsplit(url)
    with pytest.raises(OSError):
        await asyncio.open_connection(target.hostname, target.port)


def test_relay_rejects_non_http_endpoint():
    with pytest.raises(ProxyRelayError):
        LocalProxyRelay("socks5://127.0.0.1:1080", CREDENTIALS)
