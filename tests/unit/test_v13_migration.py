"""Unit tests for v13 migration: listing_state, events, and deliveries outbox (T-460)."""

from pathlib import Path
import sqlite3
import pytest

from hunter.storage.migrations.v013_events import apply_v013_migration


@pytest.mark.asyncio
async def test_v13_migration_and_constraints(tmp_path: Path) -> None:
    db_path = tmp_path / "v13_test.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    await apply_v013_migration(con, db_path)

    # 1. Nullable price state in listing_state
    con.execute(

            'INSERT INTO listing_state (avito_id, k'
            "nown_price, price_state) VALUES ('item"
            "_null', NULL, 'unknown')"

    )
    con.execute(

            'INSERT INTO listing_state (avito_id, k'
            "nown_price, price_state) VALUES ('item"
            "_active', 50000, 'active')"

    )
    con.commit()

    # 2. Only two event kinds allowed
    con.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at, old_price, new_price) "
        "VALUES ('ev_1', 'item_active', 1, 'NEW_LISTING', '2026-10-23T10:00:00Z', NULL, 50000)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(

                'INSERT INTO events (id, avito_id, revi'
                'sion, kind, observed_at, old_price, ne'
                "w_price) VALUES ('ev_bad', 'item_activ"
                "e', 2, 'INVALID_KIND', '2026-10-23T10:"
                "00:00Z', 50000, 48000)"

        )

    # 3. Same-second transitions preserved via distinct revisions
    con.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at, old_price, new_price) "
        "VALUES ('ev_2', 'item_active', 2, 'PRICE_CHANGED', '2026-10-23T10:00:00Z', 50000, 48000)"
    )
    con.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at, old_price, new_price) "
        "VALUES ('ev_3', 'item_active', 3, 'PRICE_CHANGED', '2026-10-23T10:00:00Z', 48000, 50000)"
    )
    con.commit()

    cur = con.cursor()
    cur.execute(

            'SELECT count(*) FROM events WHERE avit'
            "o_id = 'item_active' AND observed_at ="
            " '2026-10-23T10:00:00Z'"

    )
    assert cur.fetchone()[0] == 3

    # 4. Deliveries unique (user_id, event_id, channel)
    con.execute(
        "INSERT INTO deliveries (id, event_id, user_id, channel, payload_version, available_at) "
        "VALUES ('del_1', 'ev_1', 101, 'telegram', 1, '2026-10-23T10:00:00Z')"
    )
    con.commit()

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(

                'INSERT INTO deliveries (id, event_id, '
                'user_id, channel, payload_version, ava'
                "ilable_at) VALUES ('del_dup', 'ev_1', "
                "101, 'telegram', 1, '2026-10-23T10:00:"
                "00Z')"

        )

    # 5. Foreign key checks
    cur.execute("PRAGMA foreign_key_check")
    assert cur.fetchall() == []
    con.close()
