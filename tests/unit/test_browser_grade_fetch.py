import pytest
from hunter.transport.http_fetch import (
    DEFAULT_CHROME_USER_AGENT,
    DEFAULT_FIREFOX_USER_AGENT,
    HttpSlotFetcher,
    SlotFetchConfig,
)
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest, ChallengeStatus
from hunter.domain.slots import SlotSnapshot, SlotRole, SlotHealth


def test_slot_fetch_config_has_browser_user_agent():
    cfg = SlotFetchConfig()
    assert "curl" not in cfg.user_agent.lower()
    assert "Mozilla" in cfg.user_agent
    assert any(b in cfg.user_agent for b in ("Firefox", "Chrome"))


def test_http_slot_fetcher_has_client_hints_headers():
    fetcher = HttpSlotFetcher("slot-1", endpoint="direct", config=SlotFetchConfig(user_agent=DEFAULT_CHROME_USER_AGENT))
    headers = fetcher.client.headers
    assert "sec-ch-ua" in headers
    assert "sec-fetch-dest" in headers
    assert "sec-fetch-mode" in headers
    assert "Chrome" in headers["user-agent"]


def test_http_slot_fetcher_firefox_headers():
    fetcher = HttpSlotFetcher("slot-1", endpoint="direct", config=SlotFetchConfig(user_agent=DEFAULT_FIREFOX_USER_AGENT))
    headers = fetcher.client.headers
    assert "sec-ch-ua" not in headers
    assert "sec-fetch-dest" in headers
    assert "Firefox" in headers["user-agent"]


@pytest.mark.asyncio
async def test_challenge_detects_hcaptcha_as_operator_required(tmp_path):
    from unittest.mock import AsyncMock
    repo = AsyncMock()
    repo.get_slot = AsyncMock(return_value=SlotSnapshot(
        slot_id="slot-1", endpoint="direct", role=SlotRole.ACTIVE, health=SlotHealth.CHALLENGE,
        session_version=1, generation=1, updated_at="2026-10-31T00:00:00Z"
    ))
    repo.set_health = AsyncMock()
    session_store = AsyncMock()
    service = ChallengeHandoffService(slot_repo=repo, session_store=session_store)

    req = ChallengeRequest(
        slot_id="slot-1",
        challenge_type="challenge",
        html="<html><title>Доступ ограничен: проблема с IP</title><div class='h-captcha'></div></html>"
    )
    outcome = await service.handle_challenge(req)
    assert outcome.status == ChallengeStatus.OPERATOR_REQUIRED
    assert outcome.quarantined is True
