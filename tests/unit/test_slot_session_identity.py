import json
import sqlite3
from pathlib import Path

import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.migrations.v012_subscriptions import V012_SQL
from hunter.storage.migrations.v014_slots import V014_SQL
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.session_store import CorruptSessionFileError, SessionStore


@pytest.mark.asyncio
async def test_invalid_slot_id_cannot_alias_another_slots_session(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    await store.save_session(
        "slot_a", "http://proxy:8080", 1, 1, cookies=[{"name": "sid", "value": "abc"}]
    )
    with pytest.raises(ValueError):
        await store.load_session("slot_a!")
    original = await store.load_session("slot_a")
    assert original is not None and original.cookies[0]["value"] == "abc"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_slot_id", ["slot_b", None])
async def test_session_metadata_must_match_requested_slot(tmp_path: Path, bad_slot_id: str | None):
    store = SessionStore(tmp_path / "sessions")
    await store.save_session("slot_a", "http://proxy:8080", 1, 1, cookies=[])
    path = store._slot_path("slot_a")
    raw = json.loads(path.read_text())
    if bad_slot_id is None:
        del raw["metadata"]["slot_id"]
    else:
        raw["metadata"]["slot_id"] = bad_slot_id
    path.write_text(json.dumps(raw))
    with pytest.raises(CorruptSessionFileError):
        await store.load_session("slot_a")


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_id", ["slot!", "slot/../x", ""])
async def test_registration_rejects_invalid_slot_id_before_writing(tmp_path: Path, slot_id: str):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(V012_SQL + V014_SQL)
    conn.execute("ALTER TABLE slots ADD COLUMN role TEXT NOT NULL DEFAULT 'active'")
    conn.commit()
    conn.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        repo = SlotRepository(mgr, writer)
        with pytest.raises(ValueError):
            await repo.register_slot(slot_id, "http://proxy:8080")
        reader = await mgr.open_read_connection()
        try:
            count = await (await reader.execute("SELECT count(*) FROM slots")).fetchone()
            assert count[0] == 0
        finally:
            await mgr.close_reader(reader)
    finally:
        await writer.stop()
        await mgr.drain_and_close()
