"""Real isolated session/SQLite ownership for handler contract tests."""
from pathlib import Path
import sqlite3

import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.session_store import SessionStore


@pytest.fixture(name="reference_storage")
async def reference_storage(tmp_path):
    path = tmp_path / "reference.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("src/hunter/core/schema.sql").read_text())
    manager = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(manager)
    await writer.start()
    try:
        yield SlotRepository(manager, writer), SessionStore(tmp_path / "sessions")
    finally:
        await writer.stop()
        await manager.drain_and_close()
