"""Failure provenance crosses the public handoff API without guessing identity."""
from unittest.mock import AsyncMock

import httpx
import pytest

from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.challenge import ChallengeHandoffService, ChallengeOutcome, ChallengeRequest, ChallengeStatus


def service():
    slot = SlotSnapshot(slot_id="audit", endpoint="direct", role=SlotRole.ACTIVE,
                        health=SlotHealth.READY, session_version=1, generation=1,
                        updated_at="2026-11-04T00:00:00Z")
    repo = AsyncMock()
    repo.get_slot.return_value = slot
    return ChallengeHandoffService(repo, AsyncMock(), allow_browser=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,expected", [
    ("user_a", "user_a"), ("user_b", "user_b"), ("b", "user_b"),
    ("", ""), ("unrecognised", ""),
])
async def test_public_service_preserves_only_confirmed_origin_identity(identity, expected):
    handoff = service()
    handoff._execute_challenge = AsyncMock(return_value=ChallengeOutcome(
        "audit", ChallengeStatus.ERROR, False, None, True, reason="synthetic failure"))
    outcome = await handoff.handle_challenge(ChallengeRequest("audit", client_identity=identity))
    assert getattr(outcome, "client_identity", None) == expected
    assert getattr(outcome, "failure_kind", None) == "recovery_failed"


@pytest.mark.asyncio
async def test_network_error_is_typed_without_declaring_profile_invalid():
    handoff = service()
    handoff._execute_challenge = AsyncMock(side_effect=httpx.ConnectError("synthetic unavailable"))
    outcome = await handoff.handle_challenge(ChallengeRequest("audit", client_identity="user_b"))
    assert outcome.solved is False
    assert getattr(outcome, "client_identity", None) == "user_b"
    assert getattr(outcome, "failure_kind", None) == "transport_error"
