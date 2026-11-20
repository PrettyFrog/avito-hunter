"""Shared fixtures for regression tests reproducing audit findings F-001..F-085."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
from typing import AsyncGenerator, Generator

import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
def clean_db_path() -> Generator[Path, None, None]:
    """Yield a unique temporary database path and remove it afterwards."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        yield db_path
    finally:
        for ext in ("", "-wal", "-shm"):
            p = db_path.with_name(db_path.name + ext)
            if p.exists():
                try:
                    os.unlink(p)
                except OSError:
                    pass


@pytest.fixture
def initialized_db(clean_db_path: Path) -> Path:
    """Initialize a SQLite database with the full schema."""
    schema_path = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
    with open(schema_path, "r", encoding="utf-8") as sf:
        schema = sf.read()
    conn = sqlite3.connect(clean_db_path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()
    return clean_db_path


@pytest.fixture
async def initialized_storage(clean_db_path: Path) -> AsyncGenerator[tuple[StorageConnectionManager, SQLiteWriterActor], None]:
    """Provide an initialized storage connection manager and active writer actor."""
    schema_path = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
    with open(schema_path, "r", encoding="utf-8") as sf:
        schema = sf.read()
    conn = sqlite3.connect(clean_db_path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=clean_db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        yield mgr, writer
    finally:
        await writer.stop()
