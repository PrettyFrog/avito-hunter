import asyncio, sqlite3, pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from hunter.domain.slots import SlotHealth
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.session_store import SessionStore

async def _hang(*a, **k): await asyncio.sleep(10)

@pytest.fixture
async def env(tmp_path):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as c: c.executescript(Path("src/hunter/core/schema.sql").read_text())
    mgr, store = StorageConnectionManager(StorageConfig(db_path=db)), SessionStore(str(tmp_path / "sessions"))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try: yield SlotRepository(mgr, writer), store
    finally:
        await writer.stop()
        await mgr.drain_and_close()

@pytest.mark.asyncio
async def test_old_attempt_success_does_not_mask_subsequent_timeout(env):
    repo, store = env
    slot = await repo.register_slot("s-audit", "direct")
    service = ChallengeHandoffService(repo, store, http_pow_solver=AsyncMock(return_value=(True, [{"name": "u", "value": "t1"}])))
    assert (await service._attempt_http_pow(slot, ChallengeRequest("s-audit", html='{"pow_challenge":"c1"}'), "c1")).solved
    with patch.object(service, "_execute_challenge", _hang):
        out = await service._process_bounded_challenge(await repo.get_slot("s-audit"), ChallengeRequest("s-audit", timeout_sec=0.02))
    assert not out.solved and (await repo.get_slot("s-audit")).health == SlotHealth.QUARANTINED

@pytest.mark.asyncio
async def test_save_timeout_does_not_declare_solved_without_committed_session(env):
    repo, store = env
    slot = await repo.register_slot("s-save", "direct")
    service = ChallengeHandoffService(repo, store, http_pow_solver=AsyncMock(return_value=(True, [{"name": "u", "value": "t2"}])))
    with patch.object(store, "save_session", _hang):
        out = await service._process_bounded_challenge(slot, ChallengeRequest("s-save", html='{"pow_challenge":"c"}', timeout_sec=0.03))
    assert not out.solved and not store.has_session("s-save") and (await repo.get_slot("s-save")).health == SlotHealth.QUARANTINED

@pytest.mark.asyncio
async def test_benign_browser_teardown_timeout_after_commit_preserves_solved(env):
    repo, store = env
    slot = await repo.register_slot("s-td", "direct")
    service = ChallengeHandoffService(repo, store, browser_factory=AsyncMock())
    async def slow_td(s, req, receipt=None):
        ver = await service._save_challenge_state(s, None, [{"name": "u", "value": "t3"}])
        if receipt: receipt.mark_committed(ver)
        await asyncio.sleep(10)
    with patch.object(service, "_execute_challenge", slow_td):
        out = await service._process_bounded_challenge(slot, ChallengeRequest("s-td", timeout_sec=0.03))
    assert out.solved and store.has_session("s-td") and (await repo.get_slot("s-td")).health == SlotHealth.READY
