"""Exercise real curl streams against loopback, without contacting Avito."""

import asyncio
from contextlib import asynccontextmanager, suppress

import pytest

from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig


@asynccontextmanager
async def slow_server():
    connections = set()

    async def serve(reader, writer):
        connections.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
            for _ in range(1024):
                writer.write(b"x" * 1024)
                await writer.drain()
                await asyncio.sleep(0.02)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            connections.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()
        tasks = list(connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_early_exit_stops_background_download(cancel):
    pytest.importorskip("curl_cffi", reason="Exercises the optional installed curl backend")
    async with slow_server() as base:
        fetcher = HttpSlotFetcher("lifecycle", config=SlotFetchConfig(
            base_url=base, max_body_bytes=1048576 if cancel else 1024,
        ))
        response_ready = asyncio.Event()
        responses = []
        original_get = fetcher.curl_client.get

        async def capture(*args, **kwargs):
            response = await original_get(*args, **kwargs)
            responses.append(response)
            response_ready.set()
            return response

        fetcher.curl_client.get = capture
        task = asyncio.create_task(fetcher.fetch("https://www.avito.ru/test"))
        try:
            await asyncio.wait_for(response_ready.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await asyncio.wait_for(task, 2)
                assert result.error.startswith("body_size_exceeded:")
            assert responses[0].astream_task.done(), "download continues after caller exits"
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await fetcher.close()
