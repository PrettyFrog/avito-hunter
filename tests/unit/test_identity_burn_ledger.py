"""Unit tests for identity burn ledger and 65-second quarantine cap."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig
from hunter.application.poll_attempts import has_peer_identity
from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal


class MockResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code, self.text, self.body = status, text, text
        self.headers, self.cookies = {}, {}
        self.url, self.error = "https://www.avito.ru/moskva", None

    async def aiter_content(self):
        yield self.body.encode()

    async def aclose(self):
        pass


def test_quarantine_identity_caps_at_65_seconds() -> None:
    """quarantine_identity must never exceed 65.0 seconds under any circumstances."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    t0 = time.monotonic()
    
    # Passing 120s must be capped to 65s
    fetcher.quarantine_identity("user_a", duration_sec=120.0)
    assert fetcher.is_identity_quarantined("user_a") is True
    exp = fetcher._identity_quarantine_until.get("user_a", 0.0)
    assert 64.0 <= (exp - t0) <= 65.1

    # Default call without duration must be 65s
    fetcher.quarantine_identity("user_b")
    assert fetcher.is_identity_quarantined("user_b") is True
    exp_b = fetcher._identity_quarantine_until.get("user_b", 0.0)
    assert 64.0 <= (exp_b - t0) <= 65.1

    # Shorter duration below 65s is preserved
    fetcher.quarantine_identity("user_a", duration_sec=20.0)
    exp_short = fetcher._identity_quarantine_until.get("user_a", 0.0)
    assert 19.0 <= (exp_short - t0) <= 20.1


def test_quarantined_peer_is_not_available_for_failover() -> None:
    """A quarantined identity must not be reported as a usable peer."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))

    fetcher.quarantine_identity("user_a", duration_sec=65.0)

    assert has_peer_identity(fetcher, "user_a") is False
    assert has_peer_identity(fetcher, "a") is False
    assert has_peer_identity(fetcher, "user_b") is True


def test_quarantined_user_is_skipped_in_scheduled_polls() -> None:
    """When user_a is quarantined, scheduled polls must strictly route to user_b."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    assert fetcher.curl_client is not None
    assert fetcher.curl_client_b is not None

    fetcher.quarantine_identity("user_a", duration_sec=65.0)
    for _ in range(5):
        curl, cid = fetcher._select_scheduled_client()
        assert cid == "user_b"
        assert curl == fetcher.curl_client_b

    # Reverse: quarantine user_b instead
    fetcher.clear_identity_quarantine("user_a")
    fetcher.quarantine_identity("user_b", duration_sec=65.0)
    for _ in range(5):
        curl, cid = fetcher._select_scheduled_client()
        assert cid == "user_a"
        assert curl == fetcher.curl_client


def test_quarantined_user_restores_after_65_seconds() -> None:
    """After 65 seconds elapsed, quarantined identity automatically becomes healthy again."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    base_t = 1000.0

    with patch("time.monotonic", return_value=base_t):
        fetcher.quarantine_identity("user_a", duration_sec=65.0)
        assert fetcher.is_identity_quarantined("user_a") is True

    # After 30s: still quarantined
    with patch("time.monotonic", return_value=base_t + 30.0):
        assert fetcher.is_identity_quarantined("user_a") is True
        _, cid = fetcher._select_scheduled_client()
        assert cid == "user_b"

    # After 65.1s: quarantine expired, normal interleaving resumes
    with patch("time.monotonic", return_value=base_t + 65.1):
        assert fetcher.is_identity_quarantined("user_a") is False
        cids = [fetcher._select_scheduled_client()[1] for _ in range(4)]
        assert "user_a" in cids and "user_b" in cids


@pytest.mark.asyncio
async def test_fetch_curl_quarantines_identity_on_403_and_clears_on_200() -> None:
    """403 responses must automatically quarantine the responding identity for 65s."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    try:
        # Mock user_a to return 403
        with patch.object(fetcher.curl_client, "get", new=AsyncMock(return_value=MockResponse(403, "Blocked"))):
            res = await fetcher.fetch("https://www.avito.ru/moskva", client_identity="user_a")
            assert res.status_code == 403
            assert fetcher.is_identity_quarantined("user_a") is True

        # Next scheduled fetch without explicit cid must route to user_b
        with patch.object(fetcher.curl_client_b, "get", new=AsyncMock(return_value=MockResponse(200, "OK"))) as get_b:
            res2 = await fetcher.fetch("https://www.avito.ru/moskva")
            assert res2.status_code == 200
            assert res2.client_identity == "user_b"
            assert get_b.await_count == 1
            assert fetcher.is_identity_quarantined("user_b") is False
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_peer_failover_prevents_reusing_burned_identity_on_subsequent_poll() -> None:
    """When user_b gets 403 and user_a rescues with 200, next poll must NOT re-hit user_b."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    fetcher._session_index = 1  # Start pointing to user_b
    fetcher._user_b_ready = True

    asg = QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0)
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: fetcher, _fetchers={"test-slot": fetcher}),
        AsyncMock(return_value={"items_count": 1}), PollPolicy(),
        slot_signal_handler=AsyncMock(),
        challenge_service=SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=False))),
    )

    feed_html = (
        '<!DOCTYPE html><html><body><script data-mfe-state="true">'
        + '{"loaderData": {"data": {"catalog": {"items": [{"id": "1", "priceDetailed": {"value": 100}}]}}}}'
        + '</script></body></html>'
    )
    c403 = MockResponse(403, "Tokenless barrier")
    c200 = MockResponse(200, feed_html)

    try:
        with patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"), \
             patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):

            # Poll 1: user_b gets 403, peer failover to user_a gets 200
            with patch.object(fetcher.curl_client_b, "get", new=AsyncMock(return_value=c403)) as gb, \
                 patch.object(fetcher.curl_client, "get", new=AsyncMock(return_value=c200)) as ga:
                res1 = await rt.poll(asg, sequence=1)
                assert res1 is not None
                assert gb.await_count == 1
                assert ga.await_count == 1
                assert fetcher.is_identity_quarantined("user_b") is True

            # Poll 2 (scheduled poll without explicit cid, e.g. 29s later):
            # Must strictly use user_a because user_b is burned in quarantine!
            with patch.object(fetcher.curl_client_b, "get", new=AsyncMock(return_value=c403)) as gb2, \
                 patch.object(fetcher.curl_client, "get", new=AsyncMock(return_value=c200)) as ga2:
                res2 = await rt.poll(asg, sequence=2)
                assert res2 is not None
                assert gb2.await_count == 0  # user_b was NOT called!
                assert ga2.await_count == 1  # user_a was called instead!
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_peer_failover_does_not_use_quarantined_peer() -> None:
    """Peer failover must not touch an identity that is already in quarantine."""
    fetcher = HttpSlotFetcher("test-slot", endpoint="direct", config=SlotFetchConfig(interleave_sessions=True))
    fetcher._session_index = 1  # Start pointing to user_b
    fetcher._user_b_ready = True
    fetcher.quarantine_identity("user_a", duration_sec=65.0)

    asg = QueryAssignment("test-slot", "direct", "q-1", "https://www.avito.ru/moskva/tovary", 1, 0)
    rt = PublicPollRuntime(
        None, SimpleNamespace(get_fetcher=lambda *a: fetcher, _fetchers={"test-slot": fetcher}),
        AsyncMock(return_value={"items_count": 1}), PollPolicy(),
        slot_signal_handler=AsyncMock(),
        challenge_service=SimpleNamespace(handle_challenge=AsyncMock(return_value=SimpleNamespace(solved=False))),
    )

    feed_html = (
        '<!DOCTYPE html><html><body><script data-mfe-state="true">'
        + '{"loaderData": {"data": {"catalog": {"items": [{"id": "1", "priceDetailed": {"value": 100}}]}}}}'
        + '</script></body></html>'
    )
    c403 = MockResponse(403, "Tokenless session barrier")
    c200 = MockResponse(200, feed_html)

    try:
        with patch("hunter.application.public_poll_runtime.resolve_poll_target", return_value=("https://www.avito.ru/web/1/js/items", {})), \
             patch("hunter.application.public_poll_runtime.record_poll_response"), \
             patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):

            with patch.object(fetcher.curl_client_b, "get", new=AsyncMock(return_value=c403)) as gb, \
                 patch.object(fetcher.curl_client, "get", new=AsyncMock(return_value=c200)) as ga:
                res = await rt.poll(asg, sequence=1)

                assert res is None
                assert gb.await_count == 1
                assert ga.await_count == 0  # quarantined peer was NOT used
    finally:
        await fetcher.close()
