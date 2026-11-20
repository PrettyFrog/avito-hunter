from pathlib import Path

import pytest

from hunter.core.db import Database
from hunter.core.rawstore import write_raw


@pytest.mark.asyncio
async def test_upsert_and_explain(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    await db.connect()
    change = await db.upsert_listing(
        {
            "avito_id": "99",
            "url": "https://example.test/99",
            "title": "XM5",
            "price": 17000,
            "watchlist_id": "sony_xm5",
            "status": "active",
        }
    )
    assert change == "new"
    change = await db.upsert_listing(
        {
            "avito_id": "99",
            "url": "https://example.test/99",
            "title": "XM5",
            "price": 15000,
            "status": "active",
        }
    )
    assert change == "price_drop"
    events = await db.events("99")
    kinds = [e["kind"] for e in events]
    assert "first_seen" in kinds
    assert "price_drop" in kinds
    await db.close()


def test_write_raw_scrubs(tmp_path: Path) -> None:
    path = write_raw(tmp_path, "1", "card", "<p>тел +7 900 111-22-33</p>")
    assert path.suffixes[-2:] == [".html", ".gz"] or path.name.endswith(".html.gz")
    import gzip

    body = gzip.decompress(path.read_bytes()).decode()
    assert "+7" not in body


@pytest.mark.asyncio
async def test_published_at_in_listings(tmp_path: Path) -> None:
    db = Database(tmp_path / "t2.db")
    await db.connect()
    cols = [r[1] for r in await (await db._conn.execute("PRAGMA table_info(listings)")).fetchall()]
    assert "published_at" in cols

    await db.upsert_listing(
        {
            "avito_id": "100",
            "url": "https://example.test/100",
            "title": "Bose Ultra",
            "price": 20000,
            "published_at": "2026-10-21T12:00:00+00:00",
        }
    )
    listing = await db.get_listing("100")
    assert listing is not None
    assert listing.get("published_at") == "2026-10-21T12:00:00+00:00"
    await db.close()


@pytest.mark.asyncio
async def test_performance_indexes_created(tmp_path: Path) -> None:
    db = Database(tmp_path / "t3.db")
    await db.connect()
    cur = await db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    indexes = {r[0] for r in await cur.fetchall()}

    assert "idx_listing_events_avito_id" in indexes
    assert "idx_listing_events_ts" in indexes
    assert "idx_listings_watchlist_id" in indexes
    assert "idx_images_avito_id" in indexes
    await db.close()


@pytest.mark.asyncio
async def test_synthetic_sandbox_roots_isolation(tmp_path_factory: pytest.TempPathFactory) -> None:
    """S0: Verify two separate temporary roots and hermetic database isolation."""
    from tests.fixtures.arch_state import create_sandbox_db, FixedClock

    root1 = tmp_path_factory.mktemp("sandbox_a")
    root2 = tmp_path_factory.mktemp("sandbox_b")
    assert root1 != root2

    db1 = await create_sandbox_db(root1)
    db2 = await create_sandbox_db(root2)
    try:
        await db1.upsert_listing({"avito_id": "item_a", "title": "A", "price": 100, "url": "https://a"})
        await db2.upsert_listing({"avito_id": "item_b", "title": "B", "price": 200, "url": "https://b"})

        assert await db1.get_listing("item_a") is not None
        assert await db1.get_listing("item_b") is None
        assert await db2.get_listing("item_b") is not None
        assert await db2.get_listing("item_a") is None

        clock = FixedClock()
        t0 = clock.now_utc()
        clock.advance(60)
        assert (clock.now_utc() - t0).total_seconds() == 60
    finally:
        await db1.close()
        await db2.close()


def test_backup_sqlite_wal_cleanly(tmp_path: Path) -> None:
    """S0: Test transactionally consistent SQLite backup with active uncheckpointed WAL."""
    import sqlite3
    from tests.fixtures.arch_state import backup_sqlite_db

    src = tmp_path / "source.db"
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items VALUES (1, 'wal_item_1'), (2, 'wal_item_2')")
    conn.commit()

    wal = tmp_path / "source.db-wal"
    assert wal.exists()

    dest = tmp_path / "backup_dest.db"
    backup_sqlite_db(src, dest)
    conn.close()

    dest_conn = sqlite3.connect(dest)
    rows = dest_conn.execute("SELECT id, name FROM items ORDER BY id").fetchall()
    dest_conn.close()
    assert rows == [(1, "wal_item_1"), (2, "wal_item_2")]


def test_parallel_copy_temp_db_unique_paths(tmp_path: Path) -> None:
    """S0: Verify parallel copy_temp_db generates isolated paths without collisions."""
    from tools.parity.common import copy_temp_db, cleanup_temp_db

    d1 = tmp_path / "dir1"
    d2 = tmp_path / "dir2"
    p1 = copy_temp_db(dest_dir=d1)
    p2 = copy_temp_db(dest_dir=d2)
    try:
        assert p1 != p2
        assert p1.exists()
        assert p2.exists()
    finally:
        cleanup_temp_db(p1)
        cleanup_temp_db(p2)



