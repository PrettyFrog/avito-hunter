"""Local loopback proxy that adds Basic auth for an upstream HTTP proxy.

Playwright's built-in proxy credentials change Chromium's request path and make
Qrator withhold the ``ft`` fingerprint cookie. Browsers authenticate normally
through this loopback relay instead.
"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import urlsplit

from hunter.transport.proxy_credentials import ProxyCredentials


class ProxyRelayError(Exception):
    """The upstream proxy endpoint cannot be used by the relay."""


def _upstream(endpoint: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ProxyRelayError("Only plain HTTP proxy endpoints are supported")
    try:
        return parsed.hostname, parsed.port or 80
    except ValueError as exc:
        raise ProxyRelayError("Invalid proxy port") from exc


class LocalProxyRelay:
    """Forwards browser traffic to an authenticated upstream proxy via 127.0.0.1."""

    def __init__(self, endpoint: str, credentials: ProxyCredentials) -> None:
        self._host, self._port = _upstream(endpoint)
        token = base64.b64encode(
            f"{credentials.username}:{credentials.password}".encode("utf-8")
        ).decode("ascii")
        self._auth = f"Basic {token}"
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            raise ProxyRelayError("Relay is not started")
        return self._url

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self._url = f"http://127.0.0.1:{port}"
        return self._url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            upstream_reader, upstream = await asyncio.open_connection(self._host, self._port)
            if method.upper() == "CONNECT":
                if not await self._forward_connect(target, writer, upstream_reader, upstream):
                    return
            else:
                upstream.write(self._with_auth(head))
                await upstream.drain()
            await _duplex(reader, writer, upstream_reader, upstream)
        except Exception:
            pass
        finally:
            if upstream is not None:
                upstream.close()
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    def _with_auth(self, head: bytes) -> bytes:
        lines = head.split(b"\r\n")
        lines.insert(1, f"Proxy-Authorization: {self._auth}".encode("ascii"))
        return b"\r\n".join(lines)

    async def _forward_connect(
        self,
        target: str,
        client: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream: asyncio.StreamWriter,
    ) -> bool:
        request = (
            f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
            f"Proxy-Authorization: {self._auth}\r\n\r\n"
        )
        upstream.write(request.encode("latin-1"))
        await upstream.drain()
        response = await upstream_reader.readuntil(b"\r\n\r\n")
        if b" 200" not in response.split(b"\r\n", 1)[0]:
            client.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client.drain()
            return False
        client.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await client.drain()
        return True


async def _duplex(
    client_reader: asyncio.StreamReader,
    client: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream: asyncio.StreamWriter,
) -> None:
    await asyncio.gather(
        _pipe(client_reader, upstream),
        _pipe(upstream_reader, client),
    )


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
