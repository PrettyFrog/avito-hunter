from __future__ import annotations

from pathlib import Path
import pytest

from hunter.core.db import Database


@pytest.mark.asyncio
async def test_db_wal_checkpoint_and_truncate_on_close(tmp_path: Path) -> None:
    db_path = tmp_path / "test_wal.db"
    db = Database(db_path)
    await db.connect()

    # Insert some data to generate WAL frames
    for i in range(10):
        await db.upsert_listing(
            {
                "avito_id": str(1000 + i),
                "url": f"https://example.test/{i}",
                "title": f"Item {i}",
                "price": 1000 + i * 100,
                "watchlist_id": "test_watch",
                "status": "active",
            }
        )

    wal_path = tmp_path / "test_wal.db-wal"
    assert wal_path.exists()
    assert wal_path.stat().st_size > 0

    # 1. Passive checkpoint
    res = await db.checkpoint("PASSIVE")
    assert res is not None
    assert res[0] == 0  # 0 = SQLITE_OK

    # 2. Close database (must truncate WAL to 0 bytes)
    await db.close()
    if wal_path.exists():
        assert wal_path.stat().st_size == 0
