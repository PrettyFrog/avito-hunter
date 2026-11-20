from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.challenge import (
    ChallengeHandoffService,
    ChallengeRequest,
    ChallengeStatus,
)
from hunter.transport.http_fetch import (
    DEFAULT_FIREFOX_USER_AGENT,
    HttpSlotFetcher,
    SlotFetchConfig,
    SlotFetchResponse,
    platform_firefox_user_agent,
)


def test_platform_firefox_user_agent_matches_platform():
    with patch("sys.platform", "linux"):
        ua_linux = platform_firefox_user_agent()
        assert "X11; Linux x86_64" in ua_linux
        assert "Firefox" in ua_linux

    with patch("sys.platform", "darwin"):
        ua_mac = platform_firefox_user_agent()
        assert "Macintosh" in ua_mac
        assert "Firefox" in ua_mac

    with patch("sys.platform", "win32"):
        ua_win = platform_firefox_user_agent()
        assert "Windows NT 10.0" in ua_win
        assert "Firefox" in ua_win


@pytest.mark.asyncio
async def test_curl_stream_graceful_recovery_when_html_present():
    fetcher = HttpSlotFetcher("slot-1", endpoint="direct")
    mock_curl = AsyncMock()

    class FakeBrokenResp:
        def __init__(self):
            self.status_code = 200
            self.url = "https://www.avito.ru/catalog"
            self.headers = {"content-type": "text/html; charset=utf-8"}
            self.cookies = {}
            self.encoding = "utf-8"
            self.astream_task = None

        async def aiter_content(self):
            yield b"<!DOCTYPE html><html><body><h1>Items</h1></body></html>"
            raise RuntimeError("curl (28) Operation timed out after 15000ms")

        async def aclose(self):
            pass

    mock_curl.get = AsyncMock(return_value=FakeBrokenResp())
    fetcher.curl_client = mock_curl

    res = await fetcher.fetch("https://www.avito.ru/catalog")
    assert res.status_code == 200
    assert "<h1>Items</h1>" in res.text
    assert res.error is None


@pytest.mark.asyncio
async def test_challenge_ip_cooldown_62_seconds():
    repo = AsyncMock()
    repo.get_slot = AsyncMock(return_value=SlotSnapshot(
        slot_id="slot-1", endpoint="direct", role=SlotRole.ACTIVE, health=SlotHealth.CHALLENGE,
        session_version=1, generation=1, updated_at="2026-10-31T00:00:00Z"
    ))
    repo.set_health = AsyncMock()
    session_store = AsyncMock()
    service = ChallengeHandoffService(
        slot_repo=repo, session_store=session_store, quarantine_duration_sec=3600.0
    )

    req = ChallengeRequest(
        slot_id="slot-1",
        challenge_type="challenge",
        html="<html><title>Доступ ограничен: проблема с IP</title><div>429 Too Many Requests</div></html>"
    )
    outcome = await service.handle_challenge(req)
    assert outcome.quarantined is True

    # Validate that quarantine duration is within ~62 seconds, NOT 3600 seconds!
    call_kwargs = repo.set_health.call_args.kwargs
    quarantined_until = call_kwargs["quarantined_until"]
    q_time = datetime.fromisoformat(quarantined_until)
    delta = (q_time - datetime.now(UTC)).total_seconds()
    assert 55.0 <= delta <= 70.0
