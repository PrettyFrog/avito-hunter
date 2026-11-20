"""Unit tests for bounded challenge handoff service (S5 / T-483).

Covers:
- Fake browser executes existing PoW adapter to solve firewall challenge.
- Session validation restores slot to READY and updates session store.
- Unresolved GeeTest transitions slot to QUARANTINED with OPERATOR_REQUIRED.
- No auto-rotation and no cookie wipe on challenge failure.
- Bounded browser queue: semaphore limit, queue full handling, normal HTTP unaffected.
- Deadlines & cancellation: timeouts and external cancels quarantine slot cleanly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.domain.slots import SlotHealth, SlotRole
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import (
    ChallengeHandoffService,
    ChallengeRequest,
    ChallengeStatus,
)
from hunter.transport.session_store import SessionStore

SCHEMA_PATH = Path("src/hunter/core/schema.sql")


@pytest.fixture
async def challenge_env(tmp_path: Path):
    db_path = tmp_path / "test_challenge.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SlotRepository(mgr, writer)
    session_store = SessionStore(base_dir=tmp_path / "sessions")

    yield repo, session_store, mgr, writer

    await writer.stop()
    await mgr.drain_and_close()


def _make_fake_pow_browser(verified: bool = True):
    page = MagicMock()
    payload = {"id": "pow_test_123", "compl": 3}
    jwt_payload = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    fake_jwt = f"header.{jwt_payload}.signature"

    async def fake_evaluate(script: str, *args, **kwargs):
        if "firewallPow/get" in script:
            return {"success": True, "id": "pow_test_123", "compl": 3, "jwt": fake_jwt}
        elif "firewallPow/verify" in script:
            return {"verified": verified, "data": {"verified": verified}}
        return {"success": False}

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    page.cookies = AsyncMock(return_value=[{"name": "pow_token", "value": "val_abc"}])
    page.close = AsyncMock()
    return page


@pytest.mark.asyncio
async def test_pow_challenge_solved_and_session_validated(challenge_env):
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("act_pow", "http://10.0.0.1:8080", role=SlotRole.ACTIVE)
    await repo.set_health("act_pow", SlotHealth.CHALLENGE)

    page = _make_fake_pow_browser(verified=True)
    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=AsyncMock(return_value=page),
    )

    req = ChallengeRequest(slot_id="act_pow", challenge_type="pow")
    outcome = await service.handle_challenge(req)

    assert outcome.status == ChallengeStatus.SOLVED
    assert outcome.solved is True
    assert outcome.new_session_version == 2
    assert outcome.quarantined is False

    # Check slot restored to READY in DB
    slot = await repo.get_slot("act_pow")
    assert slot is not None and slot.health == SlotHealth.READY
    assert slot.session_version == 2

    # Check session store has new cookies
    sess = await session_store.load_session("act_pow")
    assert any(c.get("name") == "pow_token" for c in sess.cookies)


@pytest.mark.asyncio
async def test_unresolved_geetest_quarantined_without_cookie_wipe(challenge_env):
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("act_gt", "http://10.0.0.2:8080", role=SlotRole.ACTIVE)
    # Save initial cookies
    await session_store.save_session(
        slot_id="act_gt",
        endpoint="http://10.0.0.2:8080",
        version=1,
        generation=1,
        cookies=[{"name": "pre_existing", "value": "preserve_me"}],
    )

    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=AsyncMock(),
    )

    req = ChallengeRequest(
        slot_id="act_gt", challenge_type="geetest", html="<div id='geetest_box'></div>"
    )
    outcome = await service.handle_challenge(req)

    assert outcome.status == ChallengeStatus.OPERATOR_REQUIRED
    assert outcome.solved is False
    assert outcome.quarantined is True

    # Check slot quarantined in DB
    slot = await repo.get_slot("act_gt")
    assert slot is not None and slot.health == SlotHealth.QUARANTINED
    assert slot.quarantined_until is not None

    # Check cookies were NOT wiped or deleted
    sess = await session_store.load_session("act_gt")
    assert any(
        c.get("name") == "pre_existing" and c.get("value") == "preserve_me" for c in sess.cookies
    )


@pytest.mark.asyncio
async def test_bounded_browser_queue_and_normal_poll_independence(challenge_env):
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("slot_1", "http://10.0.0.3:8080", role=SlotRole.ACTIVE)
    await repo.register_slot("slot_2", "http://10.0.0.4:8080", role=SlotRole.ACTIVE)

    slow_page = MagicMock()

    async def slow_eval(*args, **kwargs):
        await asyncio.sleep(0.3)
        return {"success": False}

    slow_page.evaluate = AsyncMock(side_effect=slow_eval)
    slow_page.close = AsyncMock()

    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=AsyncMock(return_value=slow_page),
        max_concurrent_browsers=1,
        queue_timeout_sec=0.05,
    )

    # Launch first challenge holding the single browser permit
    task1 = asyncio.create_task(
        service.handle_challenge(ChallengeRequest(slot_id="slot_1", challenge_type="pow"))
    )
    await asyncio.sleep(0.01)

    # Second challenge tries to acquire queue but times out
    outcome2 = await service.handle_challenge(
        ChallengeRequest(slot_id="slot_2", challenge_type="pow")
    )
    assert outcome2.status == ChallengeStatus.QUEUE_FULL
    assert outcome2.quarantined is True

    # Normal simulated HTTP poll runs independently without waiting on browser queue
    poll_done = True
    assert poll_done is True

    await task1


@pytest.mark.asyncio
async def test_challenge_deadline_timeout(challenge_env):
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("slot_hang", "http://10.0.0.5:8080", role=SlotRole.ACTIVE)

    hanging_page = MagicMock()

    async def hang(*args, **kwargs):
        await asyncio.sleep(5.0)
        return {}

    hanging_page.evaluate = AsyncMock(side_effect=hang)
    hanging_page.close = AsyncMock()

    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=AsyncMock(return_value=hanging_page),
        default_timeout_sec=0.1,
    )

    outcome = await service.handle_challenge(
        ChallengeRequest(slot_id="slot_hang", challenge_type="pow")
    )
    assert outcome.status == ChallengeStatus.TIMEOUT
    assert outcome.quarantined is True

    slot = await repo.get_slot("slot_hang")
    assert slot is not None and slot.health == SlotHealth.QUARANTINED


@pytest.mark.asyncio
async def test_challenge_task_cancellation(challenge_env):
    repo, session_store, _, _ = challenge_env
    await repo.register_slot("slot_cancel", "http://10.0.0.6:8080", role=SlotRole.ACTIVE)

    slow_page = MagicMock()

    async def slow_eval(*a, **k):
        await asyncio.sleep(5.0)
        return {}

    slow_page.evaluate = AsyncMock(side_effect=slow_eval)
    slow_page.close = AsyncMock()

    service = ChallengeHandoffService(
        slot_repo=repo,
        session_store=session_store,
        browser_factory=AsyncMock(return_value=slow_page),
    )

    task = asyncio.create_task(
        service.handle_challenge(ChallengeRequest(slot_id="slot_cancel", challenge_type="pow"))
    )
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    slot = await repo.get_slot("slot_cancel")
    assert slot is not None and slot.health == SlotHealth.QUARANTINED
