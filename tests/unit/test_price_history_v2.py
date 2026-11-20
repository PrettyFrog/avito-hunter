"""Unit tests for additive price history v2 with revision identity (S3 / T-469)."""

from __future__ import annotations
from pathlib import Path
import pytest
from hunter.core.db import Database
from hunter.storage.price_history import PriceHistoryRepository


@pytest.fixture
async def ph_env(tmp_path: Path):
    db = Database(tmp_path / "test_price_v2.db")
    await db.connect()
    repo = PriceHistoryRepository(db)
    yield db, repo
    await db.close()


@pytest.mark.asyncio
async def test_price_history_v2_revisions_sequential_and_aba(ph_env):
    _, repo = ph_env
    lid = "listing_aba"
    rev1 = await repo.record_price(lid, 10000, "2026-10-25T10:00:00+00:00")
    rev2 = await repo.record_price(lid, 12000, "2026-10-25T10:00:01+00:00", previous_price=10000)
    rev3 = await repo.record_price(lid, 10000, "2026-10-25T10:00:02+00:00", previous_price=12000)

    assert [rev1.revision, rev2.revision, rev3.revision] == [1, 2, 3]
    assert [rev1.price, rev2.price, rev3.price] == [10000, 12000, 10000]
    history = await repo.get_history(lid, use_v2=True)
    assert len(history) == 3
    assert [r.revision for r in history] == [1, 2, 3]


@pytest.mark.asyncio
async def test_legacy_import_without_inventing_transitions(ph_env):
    db, repo = ph_env
    conn = await db.connect()
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS price_history "
        "(listing_id TEXT, ts TEXT, price INTEGER, PRIMARY KEY (listing_id, ts))"
    )
    await conn.execute(
        "INSERT INTO price_history VALUES ('legacy_item', '2026-10-24T12:00:00', 5000)"
    )
    await conn.execute(
        "INSERT INTO price_history VALUES ('legacy_item', '2026-10-24T14:00:00', 4500)"
    )
    await conn.commit()

    assert await repo.import_legacy_rows() == 2
    v2_hist = await repo.get_history("legacy_item", use_v2=True)
    assert [r.price for r in v2_hist] == [5000, 4500]
    assert [r.revision for r in v2_hist] == [1, 2]
    assert v2_hist[1].previous_price == 5000


@pytest.mark.asyncio
async def test_switchable_read_adapter_parity(ph_env):
    db, repo = ph_env
    conn = await db.connect()
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS price_history "
        "(listing_id TEXT, ts TEXT, price INTEGER, PRIMARY KEY (listing_id, ts))"
    )
    await conn.execute(
        "INSERT INTO price_history VALUES ('item_adapt', '2026-10-25T01:00:00', 8000)"
    )
    await conn.commit()

    legacy_hist = await repo.get_history("item_adapt", use_v2=False)
    await repo.import_legacy_rows()
    v2_hist = await repo.get_history("item_adapt", use_v2=True)
    assert [r.price for r in legacy_hist] == [r.price for r in v2_hist] == [8000]
