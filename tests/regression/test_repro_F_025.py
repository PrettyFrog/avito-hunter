"""Regression test for F-025: Premature subscription_seen marking suppresses lots after filter expansion."""

from __future__ import annotations

import json
from pathlib import Path
import aiosqlite
import pytest

from hunter.domain.observations import ObservedListing, PriceState
from hunter.storage.recipient_fanout import _get_eligible_recipients


@pytest.mark.asyncio
async def test_repro_F_025_seen_premarking_suppresses_after_budget_increase(clean_db_path: Path) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
    with open(schema_path, "r", encoding="utf-8") as sf:
        schema_sql = sf.read()

    async with aiosqlite.connect(clean_db_path) as conn:
        await conn.executescript(schema_sql)
        await conn.execute("INSERT INTO users (id, role, status, created_at) VALUES (101, 'user', 'active', '2026-10-30T00:00:00Z')")
        await conn.execute(
            "INSERT INTO runtime (key, value) VALUES ('cutover:tenant:101', ?)",
            (json.dumps({"mode": "v2", "generation": 1, "cutoff_sequence": 0}),),
        )
        await conn.execute("INSERT INTO queries (id, canonical_url) VALUES ('q1', 'http://a')")
        await conn.execute(
            "INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords, enabled) "
            "VALUES ('sub_1', 101, 'q1', 10000, '', 1)"
        )
        await conn.commit()

        listing = ObservedListing("av_123", "MacBook Air M1", 15000, PriceState.ACTIVE, "http://a/123")
        info = {"id": "ev_1"}
        subs_initial = [("sub_1", 101, 10000, ())]

        # Cycle 1: Price 15,000 exceeds budget 10,000 -> not eligible
        eligible1 = await _get_eligible_recipients(conn, subs_initial, listing, 1, info, "2026-10-30T00:00:00Z", 10, None)
        await conn.commit()
        assert len(eligible1) == 0, "Item should not be eligible under budget 10,000"

        # User updates max_price to 20,000 in bot
        await conn.execute("UPDATE subscriptions SET max_price = 20000 WHERE id = 'sub_1'")
        await conn.commit()
        subs_updated = [("sub_1", 101, 20000, ())]

        # Cycle 2: Item 15,000 is still in feed, user now has budget 20,000.
        # Desired behavior: user should be eligible and receive the item!
        eligible2 = await _get_eligible_recipients(conn, subs_updated, listing, 1, info, "2026-10-30T00:01:00Z", 11, None)
        assert len(eligible2) == 1, "Expected eligible recipient after budget increase (F-025)"
