"""Late Retry-After is received after stream headers; close must drain its persistence."""
import asyncio
from contextlib import nullcontext

import pytest
from curl_cffi import CurlOpt

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_curl import MeteredCurlSession
from hunter.transport.metered_http import RequestGateway


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
async def test_trailer_cooldown_is_persisted_before_session_close_completes(write_fails):
    release = asyncio.Event()
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                         b"Trailer: Retry-After\r\nConnection: close\r\n\r\n5\r\nfirst\r\n")
            await writer.drain()
            await release.wait()
            writer.write(b"0\r\nRetry-After: 300\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    saved = []
    class Store:
        async def load(self, *args):
            return None
        async def extend(self, group, upstream, **kwargs):
            saved.append((upstream, kwargs["duration_sec"]))
            if write_fails:
                raise OSError("synthetic disk failure")
    budget = RequestBudget(enforce=True, clock=lambda: 10)
    try:
        with (pytest.raises(OSError) if write_fails else nullcontext()):
            async with MeteredCurlSession(gateway=RequestGateway(budget, cooldown_store=Store()),
                egress_group="shared", generation=1, emit=lambda event: None,
                curl_options={CurlOpt.NOPROXY: "*"}) as client:
                response = await client.get(f"http://127.0.0.1:{port}/", stream=True)
                assert saved == []
                release.set()
                assert await response.acontent() == b"first"
                await response.aclose()
        assert budget.cooldown_deadline("shared", "other") == 310
        assert saved == [("other", 300)]
        assert budget.closed is write_fails
    finally:
        release.set()
        server.close()
        await server.wait_closed()
