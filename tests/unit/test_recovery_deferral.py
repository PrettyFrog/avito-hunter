"""Local cooldown must cross recovery without mutating slot or profile health."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime
from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.metered_http import HttpDeferred


@pytest.mark.asyncio
async def test_service_deferral_does_not_quarantine_or_write_session():
    slot = SlotSnapshot(slot_id="audit", endpoint="direct", role=SlotRole.ACTIVE,
                        health=SlotHealth.READY, session_version=1, generation=1,
                        updated_at="2026-11-04T00:00:00Z")
    repo, store = AsyncMock(), AsyncMock()
    repo.get_slot.return_value = slot
    service = ChallengeHandoffService(repo, store, allow_browser=False)
    deferred = HttpDeferred("cooldown", 9000)
    service._execute_challenge = AsyncMock(side_effect=deferred)
    req = ChallengeRequest("audit", client_identity="user_b")
    for _ in range(2):
        with pytest.raises(HttpDeferred) as caught:
            await service.handle_challenge(req)
        assert caught.value is deferred
        assert not service._slot_locks["audit"].locked()
        assert not service._semaphore.locked()
    repo.set_health.assert_not_awaited()
    assert store.mock_calls == []


@pytest.mark.asyncio
async def test_runtime_deferral_reaches_worker_without_cleanup(monkeypatch):
    req = ChallengeRequest("audit", client_identity="user_b")
    monkeypatch.setattr("hunter.application.poll_attempts.prepare_challenge_request",
                        AsyncMock(return_value=req))
    cleanup = AsyncMock()
    monkeypatch.setattr("hunter.application.public_poll_runtime.cleanup_after_outcome", cleanup)
    deferred = HttpDeferred("cooldown", 9000)
    service = SimpleNamespace(handle_challenge=AsyncMock(side_effect=deferred))
    runtime = SimpleNamespace(identity_cleanup=False, http_pool=None, challenge_service=service)
    with pytest.raises(HttpDeferred) as caught:
        await PublicPollRuntime._handle_challenge(runtime, "audit", SimpleNamespace())
    assert caught.value is deferred
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_http_recovery_flow_preserves_deferred_without_commit():
    slot = SlotSnapshot(slot_id="audit", endpoint="direct", role=SlotRole.ACTIVE,
                        health=SlotHealth.READY, session_version=1, generation=1,
                        updated_at="2026-11-04T00:00:00Z")
    repo, store = AsyncMock(), AsyncMock()
    repo.get_slot.return_value = slot
    store.load_session.return_value = None
    deferred = HttpDeferred("cooldown", 9000)
    solver = AsyncMock(side_effect=deferred)
    service = ChallengeHandoffService(repo, store, allow_browser=False, http_pow_solver=solver)
    with pytest.raises(HttpDeferred) as caught:
        await service.handle_challenge(ChallengeRequest(
            "audit", client_identity="user_b", challenge_token="synthetic-token"))
    assert caught.value is deferred
    repo.set_health.assert_not_awaited()
    store.save_session.assert_not_awaited()
    assert not service._slot_locks["audit"].locked()
    assert not service._semaphore.locked()
