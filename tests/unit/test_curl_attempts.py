"""Curl debug headers must produce secret-free per-hop events."""
from curl_cffi.curl import CURLINFO_HEADER_IN, CURLINFO_HEADER_OUT
import pytest


def test_redirect_hops_are_separate_and_headers_are_not_retained():
    from hunter.transport.curl_attempts import CurlAttemptTrace
    events = []
    trace = CurlAttemptTrace(events.append)
    for path, status in [(b"/first?token=secret", 302), (b"/second", 200)]:
        trace.debug(CURLINFO_HEADER_OUT, b"GET " + path + b" HTTP/1.1\r\nCookie: secret\r\n\r\n")
        trace.debug(CURLINFO_HEADER_IN, f"HTTP/1.1 {status} OK\r\n".encode())
    trace.finish()
    assert [e["status"] for e in events if e["event"] == "http_attempt_end"] == [302, 200]
    assert len({e["attempt_id"] for e in events}) == 2
    assert "secret" not in str(events)


def test_proxy_connect_is_not_an_avito_http_request():
    from hunter.transport.curl_attempts import CurlAttemptTrace
    events = []
    trace = CurlAttemptTrace(events.append)
    trace.debug(CURLINFO_HEADER_OUT, b"CONNECT example.test:443 HTTP/1.1\r\n\r\n")
    trace.debug(CURLINFO_HEADER_IN, b"HTTP/1.1 200 Connection established\r\n")
    trace.finish()
    assert events == []


def test_unfinished_request_is_marked_as_failed_at_release():
    from hunter.transport.curl_attempts import CurlAttemptTrace
    events = []
    trace = CurlAttemptTrace(events.append)
    trace.debug(CURLINFO_HEADER_OUT, b"GET / HTTP/2\r\n\r\n")
    trace.finish()
    trace.finish()
    assert len(events) == 2
    assert events[-1]["status"] is None


def test_native_headers_include_safe_retry_after_and_no_cookies():
    from hunter.transport.curl_attempts import CurlAttemptTrace
    events = []
    trace = CurlAttemptTrace(events.append)
    trace.debug(CURLINFO_HEADER_OUT, b"GET / HTTP/1.1\r\nHost: www.avito.ru\r\n\r\n")
    for line in (b"HTTP/1.1 429 Too Many Requests\r\n", b"Retry-After: 9000\r\n",
                 b"Set-Cookie: secret\r\n", b"\r\n"):
        trace.debug(CURLINFO_HEADER_IN, line)
    trace.finish()
    assert events[-1]["retry_after_sec"] == 9000
    assert events[-1]["target_service"] == "avito"
    assert "secret" not in str(events)


@pytest.mark.asyncio
async def test_native_curl_counts_redirects_on_loopback():
    from aiohttp import web
    from hunter.transport.observed_curl import ObservedCurlSession
    events, wire = [], []
    async def handle(request):
        wire.append(request.path)
        if request.path == "/first":
            raise web.HTTPFound("/second")
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/{path}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ObservedCurlSession(emit=events.append) as session:
            response = await session.get(f"http://127.0.0.1:{port}/first")
            assert response.status_code == 200
        assert wire == ["/first", "/second"]
        assert [e["status"] for e in events if e["event"] == "http_attempt_end"] == [302, 200]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_native_connection_failure_is_an_attempt_without_sent_headers():
    import socket
    from curl_cffi.requests.exceptions import RequestException
    from hunter.transport.observed_curl import ObservedCurlSession
    events = []
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        async with ObservedCurlSession(emit=events.append) as session:
            with pytest.raises(RequestException):
                await session.get(f"http://127.0.0.1:{port}/", timeout=1)
    assert len(events) == 2
    assert events[0]["event"] == "http_attempt_start"
    assert events[-1]["event"] == "http_attempt_end"
    assert events[-1]["status"] is None
    assert events[-1]["wire_started"] is False


@pytest.mark.asyncio
async def test_parallel_curl_handles_have_independent_attempt_ids():
    import asyncio
    from aiohttp import web
    from hunter.transport.observed_curl import ObservedCurlSession
    events = []
    async def handle(request):
        await asyncio.sleep(0.01)
        return web.Response(status=403 if request.path == "/denied" else 200)
    app = web.Application()
    app.router.add_get("/{path}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ObservedCurlSession(emit=events.append) as session:
            responses = await asyncio.gather(*(session.get(f"http://127.0.0.1:{port}/{p}") for p in ("denied", "ok")))
        assert sorted(r.status_code for r in responses) == [200, 403]
        starts = [e for e in events if e["event"] == "http_attempt_start"]
        ends = [e for e in events if e["event"] == "http_attempt_end"]
        assert len(starts) == len(ends) == 2
        assert {e["attempt_id"] for e in starts} == {e["attempt_id"] for e in ends}
        assert len({e["attempt_id"] for e in starts}) == 2
        assert sorted(e["status"] for e in ends) == [200, 403]
    finally:
        await runner.cleanup()
