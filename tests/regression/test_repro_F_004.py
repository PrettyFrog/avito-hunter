"""Regression test for F-004: ChallengeHandoffService isolated from runtime."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import pytest

from hunter.application.public_poll_runtime import PublicPollRuntime, QueryAssignment
from hunter.domain.poll_policy import PollPolicy
from hunter.domain.slots import SlotSignal
from hunter.transport.challenge import ChallengeOutcome, ChallengeStatus


@pytest.mark.asyncio
async def test_repro_F_004_challenge_handoff_wired_to_runtime() -> None:
    challenge_service = MagicMock()
    challenge_service.handle_challenge = AsyncMock(
        return_value=ChallengeOutcome(
            slot_id="s1", status=ChallengeStatus.SOLVED, solved=True,
            new_session_version=2, quarantined=False,
        )
    )

    signals_received: list[tuple[str, SlotSignal]] = []

    async def fake_signal_handler(slot_id: str, signal: SlotSignal, **kwargs: Any) -> None:
        signals_received.append((slot_id, signal))

    class MockChallengeResponse:
        error = None
        status_code = 200
        text = "<html><head><title>Avito</title></head><body>firewall/captcha smartcaptcha.yandexcloud.net</body></html>"
        url = "https://www.avito.ru/firewall/captcha"
        elapsed_sec = 0.05

    http_pool = MagicMock()
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(return_value=MockChallengeResponse())
    http_pool.get_fetcher.return_value = fetcher

    runtime = PublicPollRuntime(
        conn_mgr=None,
        http_pool=http_pool,
        ingest=AsyncMock(),
        policy=PollPolicy(day_interval_sec=1, night_interval_sec=1),
        slot_signal_handler=fake_signal_handler,
        challenge_service=challenge_service,
    )

    assignment = QueryAssignment("s1", "http://p1:8080", "q1", "https://www.avito.ru/q1", 1, 0)
    await runtime.poll(assignment, 0)
    await asyncio.sleep(0.01)

    assert challenge_service.handle_challenge.called, "ChallengeHandoffService was not invoked on challenge (F-004)"
    req = challenge_service.handle_challenge.call_args[0][0]
    assert req.slot_id == "s1"
    assert ("s1", SlotSignal.CHALLENGE_SOLVED) in signals_received, f"CHALLENGE_SOLVED not emitted: {signals_received} (F-004)"
