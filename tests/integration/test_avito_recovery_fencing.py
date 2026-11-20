import sqlite3, pytest
from pathlib import Path
from unittest.mock import patch
from hunter.domain.slots import SlotHealth
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.recovery_commit import guarded_recovery_commit, reconcile_uncommitted_receipts
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.session_store import SessionStore

async def _ok(**kw): return True, [{"name": "u", "value": "v"}]
def _svc(repo, store, solver=_ok): return ChallengeHandoffService(repo, store, http_pow_solver=solver)

@pytest.fixture
async def env(tmp_path: Path):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as c: c.executescript(Path("src/hunter/core/schema.sql").read_text())
    mgr = StorageConnectionManager(StorageConfig(db_path=db))
    store, writer = SessionStore(str(tmp_path / "sessions")), SQLiteWriterActor(mgr)
    await writer.start()
    try: yield SlotRepository(mgr, writer), store
    finally:
        await writer.stop()
        await mgr.drain_and_close()

@pytest.mark.asyncio
async def test_disable_during_recovery_prevents_session_write_and_ready(env):
    repo, store = env
    slot = await repo.register_slot("slot-dis", "direct")
    async def solver(**kw):
        await repo.disable_slot("slot-dis")
        return True, [{"name": "u", "value": "v"}]
    out = await _svc(repo, store, solver)._attempt_http_pow(slot, ChallengeRequest("slot-dis", endpoint="direct"), "tok")
    assert (await repo.get_slot("slot-dis")).health == SlotHealth.DISABLED and (out is None or not out.solved) and not store.has_session("slot-dis")

@pytest.mark.asyncio
async def test_reassign_generation_bump_rejects_recovery(env):
    repo, store = env
    slot = await repo.register_slot("slot-gen", "direct")
    async def solver(**kw):
        await repo.bump_generation("slot-gen")
        return True, [{"name": "u", "value": "v"}]
    out = await _svc(repo, store, solver)._attempt_http_pow(slot, ChallengeRequest("slot-gen", endpoint="direct"), "tok")
    assert (out is None or not out.solved) and not store.has_session("slot-gen")

@pytest.mark.asyncio
async def test_endpoint_mismatch_during_recovery_rejects_commit(env):
    repo, store = env
    slot = await repo.register_slot("slot-ep", "direct")
    out = await _svc(repo, store)._attempt_http_pow(slot, ChallengeRequest("slot-ep", endpoint="http://proxy.other:8080"), "tok")
    assert (out is None or not out.solved) and not store.has_session("slot-ep")

@pytest.mark.asyncio
async def test_valid_recovery_commits_and_restores_ready(env):
    repo, store = env
    slot = await repo.register_slot("slot-ok", "direct")
    out = await _svc(repo, store)._attempt_http_pow(slot, ChallengeRequest("slot-ok", endpoint="direct"), "tok")
    assert out is not None and out.solved and (await repo.get_slot("slot-ok")).health == SlotHealth.READY and store.has_session("slot-ok")

@pytest.mark.asyncio
async def test_assignment_stale_generation_rejects_commit(env):
    repo, store = env
    slot = await repo.register_slot("slot-asg", "direct")
    req = ChallengeRequest("slot-asg", endpoint="direct", query_id="q-stale", assignment_generation=999)
    out = await _svc(repo, store)._attempt_http_pow(slot, req, "tok")
    assert (out is None or not out.solved) and not store.has_session("slot-asg")


@pytest.mark.asyncio
async def test_uncommitted_cookies_not_visible_before_cas(env):
    repo, store = env
    slot = await repo.register_slot("slot-vis", "direct")
    await store.save_session("slot-vis", "direct", 1, 1, cookies=[{"name": "u", "value": "old-val"}])

    orig_submit = repo.writer.submit
    observed_cookies = []

    async def intercept(tx):
        sess = await store.load_session("slot-vis")
        observed_cookies.append(sess.cookies[0]["value"])
        return await orig_submit(tx)

    repo.writer.submit = intercept
    out = await _svc(repo, store, lambda **kw: _ok())._attempt_http_pow(
        slot, ChallengeRequest("slot-vis", endpoint="direct"), "tok"
    )
    assert out and out.solved
    assert observed_cookies[0] == "old-val", "Uncommitted cookies must not be visible before CAS"
    final_sess = await store.load_session("slot-vis")
    assert final_sess.cookies[0]["value"] == "v"


@pytest.mark.asyncio
async def test_failed_commit_preserves_old_valid_profile(env):
    repo, store = env
    slot = await repo.register_slot("slot-keep", "direct")
    await store.save_session("slot-keep", "direct", 1, 1, cookies=[{"name": "u", "value": "old-valid"}])

    async def solver(**kw):
        await repo.disable_slot("slot-keep")
        return True, [{"name": "u", "value": "late-loser"}]

    out = await _svc(repo, store, solver)._attempt_http_pow(
        slot, ChallengeRequest("slot-keep", endpoint="direct"), "tok"
    )
    assert out is None or not out.solved
    assert store.has_session("slot-keep")
    sess = await store.load_session("slot-keep")
    assert sess.cookies[0]["value"] == "old-valid", "Old valid profile must not be deleted on reject"


@pytest.mark.asyncio
async def test_failed_commit_does_not_delete_newer_winner_profile(env):
    repo, store = env
    slot = await repo.register_slot("slot-win", "direct")
    # Simulate a concurrent winner that committed version 99
    await store.save_session("slot-win", "direct", 99, 99, cookies=[{"name": "u", "value": "winner"}], force=True)

    # Stale request trying to recover with mismatched assignment generation
    req = ChallengeRequest("slot-win", endpoint="direct", query_id="missing-q", assignment_generation=999)
    out = await _svc(repo, store)._attempt_http_pow(slot, req, "tok")
    assert out is None or not out.solved
    assert store.has_session("slot-win")
    sess = await store.load_session("slot-win")
    assert sess.cookies[0]["value"] == "winner", "Newer winner profile must not be deleted"


@pytest.mark.asyncio
async def test_activation_rejected_when_assignment_unassigned_after_reserve(env):
    repo, store = env
    await repo.register_slot("slot-unassign", "direct")
    asg_gen = await repo.assign_query("slot-unassign", "q_fence")
    slot = await repo.get_slot("slot-unassign")
    original_save = store.save_session

    async def interleave(*args, **kwargs):
        res = await original_save(*args, **kwargs)
        await repo.unassign_query("slot-unassign", "q_fence")
        return res

    with patch.object(store, "save_session", interleave):
        commit_res = await guarded_recovery_commit(
            repo, store, slot, [{"name": "u", "value": "late"}],
            query_id="q_fence", assignment_generation=asg_gen,
        )
    assert commit_res is None


@pytest.mark.asyncio
async def test_compensation_cas_skips_bumped_generation(env):
    repo, store = env
    slot = await repo.register_slot("slot-cas", "direct")
    original_get = repo.get_slot
    reads = 0

    async def interleave(slot_id):
        nonlocal reads
        snapshot = await original_get(slot_id)
        reads += 1
        if reads == 2:
            await repo.bump_generation("slot-cas")
        return snapshot

    async def fail_save(*args, **kwargs):
        raise OSError("disk write failed")

    with patch.object(repo, "get_slot", interleave), patch.object(store, "save_session", fail_save):
        with pytest.raises(OSError):
            await guarded_recovery_commit(repo, store, slot, [{"name": "u", "value": "late"}])

    cur = await original_get("slot-cas")
    assert cur.generation == 2
    assert cur.health == SlotHealth.READY


@pytest.mark.asyncio
async def test_compensation_cas_skips_when_newer_version_won(env):
    repo, store = env
    slot = await repo.register_slot("slot-winner-comp", "direct")
    await store.save_session("slot-winner-comp", "direct", 1, 1, cookies=[{"name": "u", "value": "v1"}])
    original_save = store.save_session
    count, winner_ver = 0, None

    async def interleave(*args, **kwargs):
        nonlocal count, winner_ver
        count += 1
        if count == 1:
            winner_ver = await guarded_recovery_commit(
                repo, store, slot, [{"name": "u", "value": "winner-cookie"}]
            )
            raise OSError("simulated older attempt I/O error")
        return await original_save(*args, **kwargs)

    with patch.object(store, "save_session", interleave):
        with pytest.raises(OSError):
            await guarded_recovery_commit(repo, store, slot, [{"name": "u", "value": "loser-cookie"}])

    cur = await repo.get_slot("slot-winner-comp")
    saved = await store.load_session("slot-winner-comp")
    assert cur.health == SlotHealth.READY
    assert cur.session_version == winner_ver
    assert saved.cookies[0]["value"] == "winner-cookie"


def test_reconcile_uncommitted_receipts_quarantines_ready_slots_without_session(tmp_path):
    db_path = tmp_path / "hunter.db"
    with sqlite3.connect(db_path) as c:
        c.executescript(Path("src/hunter/core/schema.sql").read_text())
        c.execute(
            "INSERT INTO slots (id, endpoint, health_state, active_generation, cookie_namespace, created_at, updated_at) "
            "VALUES ('orphan_slot', 'direct', 'READY', 1, 'ns', datetime('now'), datetime('now'))"
        )
        c.commit()

    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)
    (s_dir / ".tmp_slot_orphan_slot.json").write_text('{"uncommitted": true}')

    store = SessionStore(s_dir)
    reconcile_uncommitted_receipts(store, db_path)

    with sqlite3.connect(db_path) as c:
        row = c.execute("SELECT health_state FROM slots WHERE id = 'orphan_slot'").fetchone()
        assert row[0] == "QUARANTINED"


def test_reconcile_accepts_committed_b_session_without_a(tmp_path):
    import json
    db_path = tmp_path / "hunter.db"
    with sqlite3.connect(db_path) as c:
        c.executescript(Path("src/hunter/core/schema.sql").read_text())
        c.execute(
            "INSERT INTO slots (id, endpoint, health_state, active_generation, cookie_namespace, created_at, updated_at) "
            "VALUES ('slot_b_only', 'direct', 'READY', 1, 'ns', datetime('now'), datetime('now'))"
        )
        c.execute(
            "INSERT INTO session_versions (slot_id, version, updated_at) VALUES ('slot_b_only', 2, datetime('now'))"
        )
        c.commit()

    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {"slot_id": "slot_b_only__user_b", "endpoint": "direct", "version": 2, "generation": 1,
                     "cookie_hash": "h", "updated_at": "ts"},
        "cookies": [{"name": "u", "value": "val_b"}],
    }
    (s_dir / "slot_slot_b_only__user_b_session.json").write_text(json.dumps(payload))

    store = SessionStore(s_dir)
    reconcile_uncommitted_receipts(store, db_path)

    with sqlite3.connect(db_path) as c:
        row = c.execute("SELECT health_state FROM slots WHERE id = 'slot_b_only'").fetchone()
        assert row[0] == "READY", "Valid committed B session must not be quarantined"


def test_reconcile_explicit_db_never_writes_neighbor(tmp_path):
    schema = Path("src/hunter/core/schema.sql").read_text()
    for name in ("owned.db", "neighbor.db"):
        with sqlite3.connect(tmp_path / name) as conn:
            conn.executescript(schema)
            conn.execute(
                "INSERT INTO slots (id, endpoint, health_state, active_generation, cookie_namespace, "
                "created_at, updated_at) VALUES ('foreign', 'direct', 'READY', 1, 'ns', "
                "datetime('now'), datetime('now'))"
            )
            conn.execute(
                "INSERT INTO session_versions (slot_id, version, updated_at) "
                "VALUES ('foreign', 2, datetime('now'))"
            )
    store = SessionStore(tmp_path / "sessions")
    reconcile_uncommitted_receipts(store, tmp_path / "owned.db")
    with sqlite3.connect(tmp_path / "neighbor.db") as conn:
        assert conn.execute("SELECT health_state FROM slots WHERE id='foreign'").fetchone()[0] == "READY"


def test_reconcile_missing_database_preserves_receipt(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    receipt = store.base_dir / ".tmp_slot_audit__uncommitted_2.json"
    receipt.write_text('{"slot_id":"audit","version":2}')
    with pytest.raises(FileNotFoundError):
        reconcile_uncommitted_receipts(store, tmp_path / "missing.db")
    assert receipt.exists()

