"""Unit tests for independent delivery dispatcher (T-485)."""

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from hunter.application.dispatcher import (
    TELEGRAM_CHAT_INTERVAL_SECONDS,
    DeliveryDispatcher,
    SendResult,
)
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import DeliveryRepository
from hunter.storage.writer import SQLiteWriterActor

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "dispatch_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = DeliveryRepository(mgr, writer)
    yield repo, writer, mgr
    await writer.stop()
    await mgr.drain_and_close()


async def _add_del(
    writer: SQLiteWriterActor,
    did: str,
    uid: int,
    avail_at: str,
    status: str = "pending",
    lease_until: str = "",
) -> None:
    eid, payload = f"ev_{did}", json.dumps({"title": "Test Item", "price": 1000})

    async def _tx(c: aiosqlite.Connection):
        await c.execute(
            (
                "INSERT OR IGNORE INTO events (id, avito_id, revision, kind, observed_at) "
                "VALUES (?, ?, 1, 'NEW_LISTING', 't')"
            ),
            (eid, f"lot_{did}"),
        )
        sql = (
            "INSERT INTO deliveries (id, event_id, user_id, channel, payload_version, payload, "
            "status, available_at, lease_until) VALUES (?, ?, ?, 'telegram', 1, ?, ?, ?, ?)"
        )
        await c.execute(sql, (did, eid, uid, payload, status, avail_at, lease_until))

    await writer.submit(_tx)


async def _row(mgr: StorageConnectionManager, sql: str) -> tuple | None:
    r = await mgr.open_read_connection()
    try:
        return await (await r.execute(sql)).fetchone()
    finally:
        await mgr.close_reader(r)


@pytest.mark.asyncio
async def test_429_one_recipient_does_not_block_others(env) -> None:
    repo, writer, mgr = env
    t0 = datetime.now(UTC).isoformat()
    await _add_del(writer, "d_u1", 101, t0)
    await _add_del(writer, "d_u2", 102, t0)

    async def mock_sender(d):
        return (
            SendResult(status="retry", retry_after_seconds=60)
            if d.user_id == 101
            else SendResult(status="sent", message_id="tg_102")
        )

    dispatcher = DeliveryDispatcher(repo, {"telegram": mock_sender})
    assert await dispatcher.dispatch_once(t0) == 2
    r1 = await _row(mgr, "SELECT status FROM deliveries WHERE id = 'd_u1'")
    r2 = await _row(mgr, "SELECT status, message_id FROM deliveries WHERE id = 'd_u2'")
    assert r1[0] == "pending" and r2[0] == "sent" and r2[1] == "tg_102"


@pytest.mark.asyncio
async def test_send_timeout_classified_as_ambiguous(env) -> None:
    repo, writer, mgr = env
    t0 = datetime.now(UTC).isoformat()
    await _add_del(writer, "d_timeout", 201, t0)

    async def timeout_sender(d):
        raise TimeoutError("Network timeout")

    dispatcher = DeliveryDispatcher(repo, {"telegram": timeout_sender})
    await dispatcher.dispatch_once(t0)
    row = await _row(mgr, "SELECT status, attempt FROM deliveries WHERE id = 'd_timeout'")
    assert row[0] == "pending" and row[1] == 1


@pytest.mark.asyncio
async def test_fault_transport_retry_does_not_create_telegram_cooldown(env) -> None:
    repo, writer, mgr = env
    now = datetime.now(UTC).isoformat()
    await _add_del(writer, "transport_retry", 2026, now)

    async def unavailable(_delivery):
        raise ConnectionError("relay unavailable")

    await DeliveryDispatcher(repo, {"telegram": unavailable}).dispatch_once(now)
    cooldowns = await _row(
        mgr,
        "SELECT count(*) FROM runtime WHERE key LIKE 'delivery:telegram:%'",
    )
    row = await _row(
        mgr, "SELECT status, attempt FROM deliveries WHERE id='transport_retry'"
    )
    assert cooldowns[0] == 0
    assert row[0] == "pending" and row[1] == 1


@pytest.mark.asyncio
async def test_lot_order_and_restart_recovery(env) -> None:
    repo, writer, mgr = env
    past_ts, now_ts = "2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00"
    await _add_del(writer, "d_crashed", 301, past_ts, status="leased", lease_until=past_ts)

    dispatcher = DeliveryDispatcher(
        repo,
        {
            "telegram": lambda d: asyncio.sleep(
                0, SendResult(status="sent", message_id="msg_recovered")
            )
        },
    )
    assert await dispatcher.dispatch_once(now_ts) == 1
    row = await _row(mgr, "SELECT status, message_id FROM deliveries WHERE id = 'd_crashed'")
    assert row[0] == "sent" and row[1] == "msg_recovered"


@pytest.mark.asyncio
async def test_wake_up_loop(env) -> None:
    repo, writer, mgr = env
    t0 = datetime.now(UTC).isoformat()
    await _add_del(writer, "d_wake", 401, t0)
    dispatched = asyncio.Event()

    async def mock_sender(d):
        dispatched.set()
        return SendResult(status="sent", message_id="w_ok")

    dispatcher = DeliveryDispatcher(repo, {"telegram": mock_sender})
    dispatcher.start(poll_interval=10.0)
    try:
        dispatcher.wake_up()
        await asyncio.wait_for(dispatched.wait(), timeout=3.0)
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_fault_telegram_global_rate_limit_staggers_distinct_users(env) -> None:
    repo, writer, _ = env
    now = datetime.now(UTC).isoformat()
    for i in range(26):
        await _add_del(writer, f"burst_{i}", 5000 + i, now)
    sent_at: list[float] = []

    async def sender(_delivery):
        sent_at.append(time.monotonic())
        return SendResult("sent", "ok")

    dispatcher = DeliveryDispatcher(repo, {"telegram": sender}, batch_size=30)
    assert await dispatcher.dispatch_once(now) == 26
    assert len(sent_at) == 26
    assert sent_at[-1] - sent_at[0] >= 1.0


@pytest.mark.asyncio
async def test_fault_telegram_chat_limit_spaces_same_recipient(env) -> None:
    repo, writer, _ = env
    now = datetime.now(UTC).isoformat()

    async def bind_shared_chat(conn: aiosqlite.Connection) -> None:
        for uid in (6001, 6002):
            await conn.execute(
                "INSERT INTO users (id, created_at) VALUES (?, ?)", (uid, now)
            )
            await conn.execute(
                "INSERT INTO channel_destinations "
                "(user_id, channel, address, verified, version) VALUES (?, 'telegram', '77', 1, 1)",
                (uid,),
            )

    await writer.submit(bind_shared_chat)
    for did, uid in (("same_chat_0", 6001), ("same_chat_1", 6002), ("same_chat_2", 6001)):
        await _add_del(writer, did, uid, now)
    sent_at: list[float] = []

    async def sender(_delivery):
        sent_at.append(time.monotonic())
        return SendResult("sent", "ok")

    dispatcher = DeliveryDispatcher(repo, {"telegram": sender}, batch_size=10)
    assert await dispatcher.dispatch_once(now) == 3
    assert len(sent_at) == 3
    assert sent_at[-1] - sent_at[0] >= 2 * TELEGRAM_CHAT_INTERVAL_SECONDS - 0.05


@pytest.mark.asyncio
async def test_fault_global_retry_after_is_persisted_for_restart(env) -> None:
    repo, writer, mgr = env
    now = datetime.now(UTC).isoformat()
    await _add_del(writer, "global_429", 7001, now)

    async def rate_limited(_delivery):
        return SendResult("retry", retry_after_seconds=45, retry_scope="global")

    dispatcher = DeliveryDispatcher(repo, {"telegram": rate_limited})
    await dispatcher.dispatch_once(now)
    cooldown = await _row(mgr, "SELECT value FROM runtime WHERE key='delivery:telegram:global'")
    assert cooldown is not None
    assert datetime.fromisoformat(cooldown[0]) > datetime.now(UTC)
    await _add_del(writer, "after_global_429", 7002, now)
    restarted = DeliveryDispatcher(repo, {"telegram": rate_limited})
    assert await restarted.dispatch_once(now) == 0
    row = await _row(mgr, "SELECT status, attempt FROM deliveries WHERE id='after_global_429'")
    assert row[0] == "pending" and row[1] == 0


@pytest.mark.asyncio
async def test_fault_chat_retry_after_is_shared_after_dispatcher_restart(env) -> None:
    repo, writer, mgr = env
    now = datetime.now(UTC).isoformat()
    await _add_del(writer, "chat_429", 7101, now)
    calls = 0

    async def rate_limited(_delivery):
        nonlocal calls
        calls += 1
        return SendResult("retry", retry_after_seconds=45, retry_scope="chat")

    await DeliveryDispatcher(repo, {"telegram": rate_limited}).dispatch_once(now)
    await _add_del(writer, "same_chat_after_429", 7101, now)
    restarted = DeliveryDispatcher(repo, {"telegram": rate_limited})
    assert await restarted.dispatch_once(now) == 1
    cooldown = await _row(
        mgr, "SELECT value FROM runtime WHERE key='delivery:telegram:user:7101'"
    )
    row = await _row(
        mgr, "SELECT status, attempt, available_at FROM deliveries "
        "WHERE id='same_chat_after_429'"
    )
    assert cooldown is not None and datetime.fromisoformat(cooldown[0]) > datetime.now(UTC)
    assert row[0] == "pending" and row[1] == 1
    assert datetime.fromisoformat(row[2]) > datetime.now(UTC)
    assert calls == 1


@pytest.mark.asyncio
async def test_quiet_digest_payload_stays_pending_until_ack(env) -> None:
    repo, writer, mgr = env
    now = datetime.now(UTC)
    later = (now + timedelta(hours=1)).isoformat()
    await _add_del(writer, "quiet_digest", 8001, later)
    payload = json.dumps({"digest_batch": ["lot-a", "lot-b"]})
    await writer.submit(
        lambda c: c.execute(
            "UPDATE deliveries SET payload=? WHERE id='quiet_digest'", (payload,)
        )
    )
    calls = 0

    async def retry_then_ack(_delivery):
        nonlocal calls
        calls += 1
        return (
            SendResult("retry", retry_after_seconds=1, retry_scope="delivery")
            if calls == 1
            else SendResult("sent", "digest-ack")
        )

    dispatcher = DeliveryDispatcher(repo, {"telegram": retry_then_ack})
    assert await dispatcher.dispatch_once(now.isoformat()) == 0
    assert calls == 0
    quiet = await _row(mgr, "SELECT status, payload FROM deliveries WHERE id='quiet_digest'")
    assert quiet[0] == "pending" and quiet[1] == payload

    await writer.submit(
        lambda c: c.execute(
            "UPDATE deliveries SET available_at=? WHERE id='quiet_digest'",
            (now.isoformat(),),
        )
    )
    assert await dispatcher.dispatch_once(now.isoformat()) == 1
    pending = await _row(mgr, "SELECT status, payload FROM deliveries WHERE id='quiet_digest'")
    assert pending[0] == "pending" and pending[1] == payload

    await writer.submit(
        lambda c: c.execute(
            "UPDATE deliveries SET available_at=? WHERE id='quiet_digest'",
            (now.isoformat(),),
        )
    )
    assert await dispatcher.dispatch_once(now.isoformat()) == 1
    sent = await _row(
        mgr,
        "SELECT status, message_id, payload FROM deliveries WHERE id='quiet_digest'",
    )
    assert sent[0] == "sent" and sent[1] == "digest-ack" and sent[2] == payload


@pytest.mark.asyncio
async def test_dispatcher_stop_closes_senders() -> None:
    from unittest.mock import AsyncMock, MagicMock

    mock_sender = AsyncMock()
    mock_sender.aclose = AsyncMock()
    disp = DeliveryDispatcher(
        repo=MagicMock(),
        senders={"telegram": mock_sender},
    )
    await disp.stop()
    mock_sender.aclose.assert_awaited_once()
