"""Integration tests for HTTP-only recovery fence in public mode (T-981)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.challenge import ChallengeHandoffService, ChallengeOutcome, ChallengeRequest, ChallengeStatus


@pytest.mark.asyncio
async def test_bootstrap_fences_browser_factory_when_http_only() -> None:
    """When public_v2 is enabled and browser_fetch=False, browser_factory is never wired to recovery."""
    settings = Settings()
    settings.public_v2.enabled = True
    settings.public_v2.browser_fetch = False

    container = Bootstrap(settings=settings)
    container.browser_factory = AsyncMock()
    container.db = MagicMock()
    container.writer = MagicMock()
    container.conn_mgr = MagicMock()
    container.slot_repo = MagicMock()
    container.session_store = MagicMock()
    container.delivery_repo = MagicMock()
    container.senders = []
    container.workers = {}

    await container._wire_services()

    assert container.challenge_service.browser_factory is None or getattr(container.challenge_service, "allow_browser", True) is False, (
        "Browser factory must not be wired to challenge_service when browser_fetch=False!"
    )


@pytest.mark.asyncio
async def test_failed_http_pow_quarantines_without_browser_spawn() -> None:
    """When allow_browser=False, failed HTTP PoW must not call browser factory and must quarantine slot."""
    slot_repo = MagicMock()
    slot = SlotSnapshot(slot_id="s1", endpoint="direct", health=SlotHealth.READY, role=SlotRole.ACTIVE, session_version=1)
    slot_repo.get_slot = AsyncMock(return_value=slot)
    slot_repo.set_health = AsyncMock()

    session_store = MagicMock()
    browser_mock = AsyncMock()

    svc = ChallengeHandoffService(
        slot_repo=slot_repo,
        session_store=session_store,
        browser_factory=browser_mock,
        http_pow_solver=AsyncMock(return_value=(False, [])),
        allow_browser=False,
    )

    req = ChallengeRequest(
        slot_id="s1",
        challenge_type="pow",
        challenge_token="dummy_token_123",
        html="<html>pow_challenge='dummy_token_123'</html>",
    )

    outcome: ChallengeOutcome = await svc.handle_challenge(req)

    browser_mock.assert_not_called()
    assert outcome.solved is False
    assert outcome.quarantined is True
    slot_repo.set_health.assert_called()


@pytest.mark.asyncio
async def test_explicit_browser_enabled_preserves_factory() -> None:
    """When browser_fetch=True, browser_factory is retained."""
    settings = Settings()
    settings.public_v2.enabled = True
    settings.public_v2.browser_fetch = True

    container = Bootstrap(settings=settings)
    dummy_factory = AsyncMock()
    container.browser_factory = dummy_factory
    container.db = MagicMock()
    container.writer = MagicMock()
    container.conn_mgr = MagicMock()
    container.slot_repo = MagicMock()
    container.session_store = MagicMock()
    container.delivery_repo = MagicMock()
    container.senders = []
    container.workers = {}

    await container._wire_services()

    assert container.challenge_service.browser_factory is dummy_factory
