"""Regression test for challenge resolution avoiding spurious quarantine alerts and worker cancellation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.application.slot_supervisor import SlotSupervisor
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotCapacity, SlotHealth, SlotRole, SlotSignal, SlotSnapshot
from hunter.transport.challenge import (
    ChallengeHandoffService,
    ChallengeOutcome,
    ChallengeRequest,
    ChallengeStatus,
)


@pytest.mark.asyncio
async def test_challenge_solved_does_not_call_slot_failure_and_keeps_worker_running() -> None:
    challenge_service = MagicMock()
    challenge_service.handle_challenge = AsyncMock(
        return_value=ChallengeOutcome(
            slot_id="slot-home",
            status=ChallengeStatus.SOLVED,
            solved=True,
            new_session_version=2,
            quarantined=False,
        )
    )

    signals_received: list[tuple[str, SlotSignal]] = []

    async def fake_signal_handler(slot_id: str, signal: SlotSignal, **kwargs) -> None:
        signals_received.append((slot_id, signal))

    class MockChallengeResponse:
        error = None
        status_code = 439
        text = "<html><body>firewall/captcha pow_challenge=xyz</body></html>"
        url = "https://www.avito.ru/firewall/captcha"
        elapsed_sec = 0.05

    class MockCatalogResponse:
        error = None
        status_code = 200
        text = '<html><body><div data-marker="catalog"><div data-marker="item">Item</div></div></body></html>'
        url = "https://www.avito.ru/q1"
        elapsed_sec = 0.05

    http_pool = MagicMock()
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=[MockChallengeResponse(), MockCatalogResponse()])
    http_pool.get_fetcher.return_value = fetcher

    runtime = PublicPollRuntime(
        conn_mgr=None,
        http_pool=http_pool,
        ingest=AsyncMock(),
        policy=PollPolicy(day_interval_sec=1, night_interval_sec=1),
        slot_signal_handler=fake_signal_handler,
        challenge_service=challenge_service,
    )

    assignment = QueryAssignment("slot-home", "direct", "q1", "https://www.avito.ru/q1", 1, 0)
    await runtime.poll(assignment, 0)

    # 1. Challenge was handled
    assert challenge_service.handle_challenge.called
    # 2. Only CHALLENGE_SOLVED was emitted, NEVER CHALLENGE_DETECTED or false failure
    assert ("slot-home", SlotSignal.CHALLENGE_SOLVED) in signals_received
    assert ("slot-home", SlotSignal.CHALLENGE_DETECTED) not in signals_received


@pytest.mark.asyncio
async def test_supervisor_does_not_cancel_worker_on_challenge_solved() -> None:
    snap = SlotSnapshot(
        slot_id="s1",
        endpoint="direct",
        health=SlotHealth.READY,
        role=SlotRole.ACTIVE,
        session_version=1,
        generation=1,
        capacity=SlotCapacity(max_queries=4, assigned_queries=("q1",)),
        consecutive_errors=0,
    )
    slot_repo = MagicMock()
    slot_repo.get_slot = AsyncMock(return_value=snap)
    slot_repo.set_health = AsyncMock()

    mock_worker = AsyncMock()
    workers = {"s1": mock_worker}
    supervisor = SlotSupervisor(slot_repo=slot_repo, workers=workers)

    outcome = await supervisor.handle_slot_signal("s1", SlotSignal.CHALLENGE_SOLVED)
    assert outcome.success is True
    assert outcome.new_health == SlotHealth.READY
    # Worker must NOT be cancelled when slot is READY
    assert mock_worker.stop.call_count == 0
