"""Integration tests for origin session retry and bounded fallback policy (T-971)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig


class MockResponse:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code, self.text, self.body = status, text, text
        self.headers, self.cookies, self.url = headers or {}, {}, "https://www.avito.ru/moskva"
        self.encoding, self.astream_task = "utf-8", None

    async def aiter_content(self): yield self.body.encode()
    async def aclose(self): pass


def _setup(session_index: int, user_b_ready: bool = True, challenge_solved: bool = False):
    f = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    f._session_index, f._user_b_ready = session_index, user_b_ready
    svc = SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=challenge_solved)))
    sig = AsyncMock()
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: f, _fetchers={"test-slot": f}),
        AsyncMock(), PollPolicy(), slot_signal_handler=sig, challenge_service=svc if challenge_solved else None,
    )
    asg = QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0)
    return f, rt, asg, sig


@pytest.mark.asyncio
async def test_challenge_retry_preserves_origin_client_b():
    f, rt, asg, _ = _setup(session_index=1, challenge_solved=True)
    f.curl_client_b.cookies.set("u", "cookie-B", domain=".avito.ru")
    feed = '{"catalog":{"items":[{"id":"1","title":"Item","price":100}]}}'
    b1, b2, a = MockResponse(439, '{"pow_challenge":"token-b"}'), MockResponse(200, feed), MockResponse(200, feed)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=a)) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(side_effect=[b1, b2])) as gb, \
             patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {"X-Requested-With": "XMLHttpRequest"})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"), \
             patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):
            await rt.poll(asg, sequence=1)
            assert gb.await_count == 2 and ga.await_count == 0
    finally: await f.close()


@pytest.mark.asyncio
async def test_challenge_retry_preserves_origin_client_a():
    f, rt, asg, _ = _setup(session_index=0, challenge_solved=True)
    feed = '{"catalog":{"items":[{"id":"1","title":"Item","price":100}]}}'
    a1, a2, b = MockResponse(439, '{"pow_challenge":"token-a"}'), MockResponse(200, feed), MockResponse(200, feed)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(side_effect=[a1, a2])) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=b)) as gb, \
             patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {"X-Requested-With": "XMLHttpRequest"})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"), \
             patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):
            await rt.poll(asg, sequence=1)
            assert ga.await_count == 2 and gb.await_count == 0
    finally: await f.close()


@pytest.mark.asyncio
async def test_api_403_does_not_trigger_html_fallback_burst():
    f, rt, asg, sig = _setup(session_index=0)
    resp_403, resp_html = MockResponse(403, "Forbidden", headers={"Retry-After": "60"}), MockResponse(200, "<html>fall back</html>")
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=resp_403)) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=resp_html)) as gb, \
             patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"):
            await rt.poll(asg, sequence=1)
            assert ga.await_count == 1 and gb.await_count == 0 and sig.await_count == 1
    finally: await f.close()


@pytest.mark.asyncio
async def test_api_404_allows_bounded_html_fallback_on_same_session():
    f, rt, asg, _ = _setup(session_index=1)
    feed = '{"catalog":{"items":[{"id":"2","title":"Item 2","price":200}]}}'
    b404, b_html, a = MockResponse(404, "Not Found"), MockResponse(200, feed), MockResponse(200, feed)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(return_value=a)) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(side_effect=[b404, b_html])) as gb, \
             patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"):
            await rt.poll(asg, sequence=1)
            assert gb.await_count == 2 and ga.await_count == 0
    finally: await f.close()
