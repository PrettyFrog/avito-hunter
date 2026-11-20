"""Unit tests for symmetrical peer identity failover on challenge retry."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig
from hunter.transport.recovery_identity import peer_identity


class MockResponse:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code, self.text, self.body = status, text, text
        self.headers, self.cookies = headers or {}, {}
        self.url, self.encoding, self.astream_task, self.error = "https://www.avito.ru/moskva", "utf-8", None, None

    async def aiter_content(self):
        yield self.body.encode()

    async def aclose(self):
        pass


def _setup(session_index: int = 0):
    f = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    f._session_index, f._user_b_ready = session_index, True
    sigs, sig_kw, ingest = [], [], AsyncMock(return_value={"items_count": 1})
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: f, _fetchers={"test-slot": f}),
        ingest, PollPolicy(),
        slot_signal_handler=AsyncMock(side_effect=lambda s, sig, **kw: (sigs.append(sig), sig_kw.append(kw))),
        challenge_service=SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=True))),
    )
    return f, rt, QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0), sigs, sig_kw, ingest


FEED_HTML = (
    '<!DOCTYPE html><html><body><script data-mfe-state="true">'
    + json.dumps({"loaderData": {"data": {"catalog": {"items": [{"id": "1", "priceDetailed": {"value": 100}}]}}}})
    + '</script></body></html>'
)


async def _run_poll(rt, asg):
    with patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
         patch("hunter.application.public_poll_runtime.record_poll_response"), \
         patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):
        return await rt.poll(asg, sequence=1)


def test_peer_identity_symmetry():
    assert peer_identity("user_a") == "user_b" and peer_identity("a") == "user_b"
    assert peer_identity("user_b") == "user_a" and peer_identity("b") == "user_a"
    assert peer_identity("__user_b") == "user_a" and peer_identity(None) is None and peer_identity("unknown") is None


@pytest.mark.parametrize("idx,client_attr,peer_attr", [(0, "curl_client", "curl_client_b"), (1, "curl_client_b", "curl_client")])
@pytest.mark.asyncio
async def test_peer_failover_symmetrical(idx, client_attr, peer_attr):
    f, rt, asg, sigs, _, ingest = _setup(session_index=idx)
    c439, c403, c200 = MockResponse(439, '{"pow":1}'), MockResponse(403, '{"firewall":1}'), MockResponse(200, FEED_HTML)
    try:
        with patch.object(getattr(f, client_attr), "get", new=AsyncMock(side_effect=[c439, c403])) as ga, \
             patch.object(getattr(f, peer_attr), "get", new=AsyncMock(return_value=c200)) as gb:
            assert await _run_poll(rt, asg) is not None
            assert ga.await_count == 2 and gb.await_count == 1 and SlotSignal.HTTP_403 not in sigs and ingest.await_count == 1
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_peer_failover_both_fail_triggers_quarantine():
    f, rt, asg, sigs, _, ingest = _setup(session_index=0)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(side_effect=[MockResponse(439), MockResponse(403, "Err A")])) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=MockResponse(403, "Err B"))) as gb:
            assert await _run_poll(rt, asg) is None
            assert ga.await_count == 2 and gb.await_count == 1 and SlotSignal.HTTP_403 in sigs and ingest.await_count == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_peer_failover_single_session_fallback():
    f, rt, asg, sigs, _, ingest = _setup(session_index=0)
    f.curl_client_b = None
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(side_effect=[MockResponse(439), MockResponse(403)])) as ga:
            assert await _run_poll(rt, asg) is None
            assert ga.await_count == 2 and SlotSignal.HTTP_403 in sigs and ingest.await_count == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_peer_failover_timeout_handled_gracefully():
    f, rt, asg, sigs, _, ingest = _setup(session_index=0)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(side_effect=[MockResponse(439), MockResponse(403)])) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(side_effect=Exception("Timeout B"))) as gb:
            assert await _run_poll(rt, asg) is None
            assert ga.await_count == 2 and gb.await_count == 1 and SlotSignal.TIMEOUT in sigs and ingest.await_count == 0
    finally:
        await f.close()


@pytest.mark.asyncio
async def test_peer_failover_preserves_retry_after():
    f, rt, asg, sigs, kw, ingest = _setup(session_index=0)
    try:
        with patch.object(f.curl_client, "get", new=AsyncMock(side_effect=[MockResponse(439), MockResponse(403)])) as ga, \
             patch.object(f.curl_client_b, "get", new=AsyncMock(return_value=MockResponse(429, headers={"Retry-After": "45"}))) as gb:
            assert await _run_poll(rt, asg) is None
            assert ga.await_count == 2 and gb.await_count == 1 and SlotSignal.HTTP_429 in sigs and ingest.await_count == 0
            assert any(k.get("quarantine_duration_sec") == 45.0 for k in kw)
    finally:
        await f.close()
