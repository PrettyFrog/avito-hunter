"""Unit tests for outbox delivery repository (T-484)."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import DeliveryRepository
from hunter.storage.writer import SQLiteWriterActor

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "del_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = DeliveryRepository(mgr, writer)
    yield repo, writer, mgr, db_path
    await writer.stop()
    await mgr.drain_and_close()


async def _insert_del(
    writer: SQLiteWriterActor, did: str, avail_at: str, pver: int = 1, payload: str | None = None
) -> None:
    p = payload or json.dumps({"title": "Macbook", "price": 50000})
    eid = f"ev_{did}"

    async def _tx(c: aiosqlite.Connection):
        await c.execute(
            "INSERT OR IGNORE INTO events (id, avito_id, revision, kind, observed_at) "
            "VALUES (?, ?, 1, 'NEW_LISTING', 't')",
            (eid, f"lot_{did}"),
        )
        await c.execute(
            "INSERT INTO deliveries (id, event_id, user_id, channel, payload_version, payload, "
            "status, available_at) "
            "VALUES (?, ?, 101, 'telegram', ?, ?, 'pending', ?)",
            (did, eid, pver, p, avail_at),
        )

    await writer.submit(_tx)


@pytest.mark.asyncio
async def test_double_claim_prevented_and_old_ack_rejected(env) -> None:
    repo, writer, mgr, _ = env
    t0 = datetime.now(UTC).isoformat()
    await _insert_del(writer, "del_1", t0)

    # Worker A claims del_1 with lease 5 seconds
    claimed_a = await repo.claim_due(t0, limit=10, lease_seconds=5)
    assert len(claimed_a) == 1
    token_a = claimed_a[0].lease_token

    # Worker B tries immediate double claim -> gets 0
    claimed_b = await repo.claim_due(t0, limit=10, lease_seconds=5)
    assert len(claimed_b) == 0

    # Simulate lease expiry: advance past lease_until
    t_expired = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    claimed_c = await repo.claim_due(t_expired, limit=10, lease_seconds=5)
    assert len(claimed_c) == 1
    token_c = claimed_c[0].lease_token
    assert token_c != token_a

    # Old ack from Worker A fails
    assert await repo.complete("del_1", token_a, message_id="msg_old") is False

    # New ack from Worker C succeeds
    assert await repo.complete("del_1", token_c, message_id="msg_new") is True


@pytest.mark.asyncio
async def test_malformed_payload_and_retry_after(env) -> None:
    repo, writer, mgr, _ = env
    t0 = datetime.now(UTC).isoformat()

    # 1. Malformed payload
    await _insert_del(writer, "del_bad", t0, pver=1, payload="{corrupted json")
    res_bad = await repo.claim_due(t0)
    assert len(res_bad) == 0

    reader = await mgr.open_read_connection()
    c = await reader.execute("SELECT status FROM deliveries WHERE id = 'del_bad'")
    assert (await c.fetchone())[0] == "dead_letter"

    # 2. RetryAfter preservation
    await _insert_del(writer, "del_retry", t0)
    claimed = await repo.claim_due(t0)
    assert len(claimed) == 1

    # Retry after 60s
    assert await repo.retry("del_retry", claimed[0].lease_token, retry_after_seconds=60) is True

    # Immediate poll returns 0
    assert len(await repo.claim_due(t0)) == 0

    # Poll 70s in the future claims it
    t_future = (datetime.now(UTC) + timedelta(seconds=70)).isoformat()
    res_fut = await repo.claim_due(t_future)
    assert len(res_fut) == 1
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_explain_query_uses_due_index(env) -> None:
    _, _, mgr, _ = env
    reader = await mgr.open_read_connection()
    sql = (
        "EXPLAIN QUERY PLAN SELECT id FROM deliveries WHERE "
        "(status = 'pending' AND available_at <= '2026-10-23T10:00:00Z')"
    )
    cur = await reader.execute(sql)
    plan = " ".join(str(tuple(row)) for row in await cur.fetchall())
    assert "idx_deliveries_due" in plan
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_count_active_for_user_and_preservation(env) -> None:
    repo, writer, mgr, _ = env
    t0 = datetime.now(UTC).isoformat()
    await _insert_del(writer, "del_u1_a", t0)
    await _insert_del(writer, "del_u1_b", t0)

    # User 101 has 2 pending
    assert await repo.count_active_for_user(101) == 2
    assert await repo.count_active_for_user(999) == 0

    # Claim one
    claimed = await repo.claim_due(t0, limit=1)
    assert len(claimed) == 1
    # Still 2 active (1 leased, 1 pending)
    assert await repo.count_active_for_user(101) == 2

    # Complete the claimed one
    assert await repo.complete(claimed[0].id, claimed[0].lease_token, "msg_1") is True
    # Now 1 active (1 pending)
    assert await repo.count_active_for_user(101) == 1


def test_delivery_row_type_hints_resolve() -> None:
    from typing import Any, get_type_hints

    hints = get_type_hints(DeliveryRepository._claim_delivery_row)
    assert hints["row"] is Any
