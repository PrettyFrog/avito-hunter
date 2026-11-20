"""Unit tests for recipient fanout inside observation transaction (T-465)."""

from pathlib import Path
import sqlite3
import pytest
import aiosqlite

from hunter.domain.observations import ObservationBatch, ObservedListing, ParseOutcome, PriceState
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "fanout_test.db"
    con = sqlite3.connect(db_path)
    schema_path = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"
    con.executescript(schema_path.read_text(encoding="utf-8"))
    con.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    yield writer, mgr, db_path
    await writer.stop()
    await mgr.drain_and_close()


def _batch(qid: str, lot_id: str, price: int, ts: str = "2026-10-23T10:00:00Z") -> ObservationBatch:
    listing = ObservedListing(
        lot_id, "Item Mac", price, PriceState.ACTIVE, f"https://avito.ru/{lot_id}"
    )
    return ObservationBatch("slot_1", qid, ts, ParseOutcome.OK, (listing,))


async def _add_sub(
    writer: SQLiteWriterActor, sid: str, uid: int, qid: str, pmax: int, words: str = ""
) -> None:
    async def _tx(c: aiosqlite.Connection):
        await c.execute(
            "INSERT OR IGNORE INTO queries (id, canonical_url, bootstrap_state) "
            "VALUES (?, ?, 'ready')",
            (qid, f"https://avito.ru/{qid}"),
        )
        await c.execute(
            "INSERT INTO subscriptions (id, user_id, query_id, max_price, "
            "manual_stopwords) VALUES (?, ?, ?, ?, ?)",
            (sid, uid, qid, pmax, words),
        )

    await writer.submit(_tx)


async def _set_v2(writer: SQLiteWriterActor, *user_ids: int) -> None:
    async def _tx(c: aiosqlite.Connection):
        for uid in user_ids:
            value = '{"mode":"v2","cutoff_sequence":0,"generation":1}'
            await c.execute(
                "INSERT OR REPLACE INTO runtime (key, value) VALUES (?, ?)",
                (f"cutover:tenant:{uid}", value),
            )

    await writer.submit(_tx)


@pytest.mark.asyncio
async def test_two_users_two_queries_one_lot(env) -> None:
    writer, mgr, _ = env
    await _add_sub(writer, "sub_1", 101, "q_1", 100000)
    await _add_sub(writer, "sub_2", 102, "q_2", 100000)
    await _set_v2(writer, 101, 102)

    res1 = await writer.submit(
        lambda c: execute_observation_tx(c, _batch("q_1", "lot_shared", 80000), request_sequence=1)
    )
    assert res1.deliveries_created == 1
    res2 = await writer.submit(
        lambda c: execute_observation_tx(c, _batch("q_2", "lot_shared", 80000), request_sequence=2)
    )
    assert res2.deliveries_created == 1
    res3 = await writer.submit(
        lambda c: execute_observation_tx(c, _batch("q_1", "lot_shared", 80000), request_sequence=3)
    )
    assert res3.deliveries_created == 0

    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT user_id FROM recipient_events")
    assert {r[0] for r in await cur.fetchall()} == {101, 102}
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_subscription_baseline_suppresses_initial(env) -> None:
    writer, mgr, _ = env

    async def _setup(c: aiosqlite.Connection):
        await c.execute(
            "INSERT INTO queries (id, canonical_url, bootstrap_state) "
            "VALUES ('q_boot', 'https://avito.ru/q', 'pending')"
        )
        await c.execute(
            "INSERT INTO subscriptions (id, user_id, query_id, max_price) "
            "VALUES ('sub_b', 201, 'q_boot', 50000)"
        )

    await writer.submit(_setup)
    await _set_v2(writer, 201)

    res1 = await writer.submit(
        lambda c: execute_observation_tx(c, _batch("q_boot", "lot_old", 30000), request_sequence=1)
    )
    assert res1.is_bootstrap and res1.deliveries_created == 0
    res2 = await writer.submit(
        lambda c: execute_observation_tx(c, _batch("q_boot", "lot_new", 35000), request_sequence=2)
    )
    assert not res2.is_bootstrap and res2.deliveries_created == 1


@pytest.mark.asyncio
async def test_cap_crossing_triggers_when_price_drops(env) -> None:
    writer, mgr, _ = env
    await _add_sub(writer, "sub_cap", 301, "q_cap", 100000)
    await _set_v2(writer, 301)

    # 120k -> 0 deliveries; 90k -> 1 delivery; 110k -> 0 deliveries
    r1 = await writer.submit(
        lambda c: execute_observation_tx(
            c, _batch("q_cap", "lot_cap", 120000, "2026-10-23T10:00:00Z"), request_sequence=1
        )
    )
    assert r1.deliveries_created == 0
    r2 = await writer.submit(
        lambda c: execute_observation_tx(
            c, _batch("q_cap", "lot_cap", 90000, "2026-10-23T10:01:00Z"), request_sequence=2
        )
    )
    assert r2.deliveries_created == 1
    r3 = await writer.submit(
        lambda c: execute_observation_tx(
            c, _batch("q_cap", "lot_cap", 110000, "2026-10-23T10:02:00Z"), request_sequence=3
        )
    )
    assert r3.deliveries_created == 0


@pytest.mark.asyncio
async def test_single_user_multiple_subscriptions_no_duplicate_delivery(env) -> None:
    writer, mgr, _ = env
    await _add_sub(writer, "sub_dup1", 401, "q_dup1", 100000)
    await _add_sub(writer, "sub_dup2", 401, "q_dup2", 120000)
    await _set_v2(writer, 401)

    r1 = await writer.submit(
        lambda c: execute_observation_tx(
            c, _batch("q_dup1", "lot_single", 80000), request_sequence=1
        )
    )
    assert r1.deliveries_created == 1
    r2 = await writer.submit(
        lambda c: execute_observation_tx(
            c, _batch("q_dup2", "lot_single", 80000), request_sequence=2
        )
    )
    assert r2.deliveries_created == 0

    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT count(*) FROM deliveries WHERE user_id = 401")
    assert (await cur.fetchone())[0] == 1
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_crash_between_event_and_intent_rolls_back_all(env) -> None:
    writer, mgr, _ = env
    await _add_sub(writer, "sub_crash", 501, "q_crash", 50000)

    async def _failing_tx(c: aiosqlite.Connection):
        await execute_observation_tx(c, _batch("q_crash", "lot_crash", 40000), request_sequence=1)
        raise RuntimeError("Crash before tx commit")

    with pytest.raises(RuntimeError, match="Crash before tx commit"):
        await writer.submit(_failing_tx)

    reader = await mgr.open_read_connection()
    c1 = await reader.execute("SELECT count(*) FROM events WHERE avito_id = 'lot_crash'")
    c2 = await reader.execute("SELECT count(*) FROM deliveries WHERE event_id LIKE '%lot_crash%'")
    assert (await c1.fetchone())[0] == 0 and (await c2.fetchone())[0] == 0
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_fanout_delivery_contains_image_url(env) -> None:
    import json
    writer, _, db_path = env
    await _add_sub(writer, "sub_img", 301, "q_img", 100000)
    await _set_v2(writer, 301)
    listing = ObservedListing(
        "lot_img", "Item with photo", 50000, PriceState.ACTIVE,
        "https://avito.ru/lot_img", image_url="https://img.avito.st/image/1/test.jpg"
    )
    batch = ObservationBatch("slot_1", "q_img", "2026-10-23T10:00:00Z", ParseOutcome.OK, (listing,))
    res = await writer.submit(lambda c: execute_observation_tx(c, batch, request_sequence=1))
    assert res.deliveries_created == 1

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT payload FROM deliveries WHERE event_id = ?", ("evt_q_img_lot_img_r1",))
    row = cur.fetchone()
    con.close()
    assert row is not None
    payload = json.loads(row[0])
    assert payload.get("image_url") == "https://img.avito.st/image/1/test.jpg"


@pytest.mark.asyncio
async def test_fanout_delivery_contains_enriched_payload(env) -> None:
    import json
    writer, _, db_path = env
    async def _add_named_sub(c: aiosqlite.Connection):
        await c.execute("INSERT OR IGNORE INTO queries (id, canonical_url, bootstrap_state) VALUES ('q_enr', 'https://avito.ru/q_enr', 'ready')")
        await c.execute("INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords, name) VALUES ('sub_enr', 302, 'q_enr', 100000, '', 'DDR4')")
    await writer.submit(_add_named_sub)
    await _set_v2(writer, 302)
    listing = ObservedListing(
        "lot_enr", "Item Enriched", 50000, PriceState.ACTIVE,
        "https://avito.ru/lot_enr", image_url="https://img.avito.st/image/1/test.jpg",
        raw_snippet={
            "description": "Описание лота",
            "photos": ["https://img.avito.st/1.jpg", "https://img.avito.st/2.jpg"],
            "delivery": True,
            "seller_rating": "5,0",
            "seller_reviews": "40 отзывов",
        }
    )
    batch = ObservationBatch("slot_1", "q_enr", "2026-10-23T10:00:00Z", ParseOutcome.OK, (listing,))
    res = await writer.submit(lambda c: execute_observation_tx(c, batch, request_sequence=1))
    assert res.deliveries_created == 1

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT payload FROM deliveries WHERE event_id = ?", ("evt_q_enr_lot_enr_r1",))
    row = cur.fetchone()
    con.close()
    assert row is not None
    payload = json.loads(row[0])
    assert payload.get("search_name") == "DDR4"
    assert payload.get("description") == "Описание лота"
    assert payload.get("photos") == ["https://img.avito.st/1.jpg", "https://img.avito.st/2.jpg"]
    assert payload.get("delivery") is True
    assert payload.get("seller_rating") == "5,0"
    assert payload.get("seller_reviews") == "40 отзывов"

