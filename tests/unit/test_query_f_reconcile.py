"""Unit tests for self-healing reconcile_conflicting_f_queries."""
from pathlib import Path
import sqlite3, pytest
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.query_f_reconcile import reconcile_conflicting_f_queries
from hunter.storage.writer import SQLiteWriterActor
from hunter.domain.query_url import extract_f_price_ceiling

F_100K = "ASgBAgECAkS0wA3OqzmwwQ2I_DcBRcaaDBZ7ImZyb20iOjAsInRvIjoxMDAwMDB9"

@pytest.fixture
async def storage(tmp_path: Path):
    db_path = tmp_path / "reconcile.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(Path("src/hunter/core/schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, status, created_at) VALUES (1, 101, 101, 'user', 'active', datetime('now'))")
        conn.execute("INSERT INTO slots (id, endpoint, account_id, cookie_namespace, role, health_state) VALUES ('slot-home', 'direct', 'a', 'n', 'active', 'READY')")
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        yield writer
    finally:
        await writer.stop()
        await mgr.drain_and_close()

@pytest.mark.asyncio
async def test_reconcile_conflicting_f_query_updates_subscription_and_assignment(storage):
    writer = storage
    old_url = f"https://www.avito.ru/all/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc?cd=1&f={F_100K}&s=104&pmax=30000"
    old_qid = "q_b3781cd1d00568b3"

    async def _seed(conn):
        await conn.execute("INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES (?, ?, 'ready')", (old_qid, old_url))
        await conn.execute("INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords, name, enabled, version) VALUES ('sub_1', 1, ?, 30000, '', 'iphone', 1, 1)", (old_qid,))
        await conn.execute("INSERT INTO assignments (id, slot_id, query_id, position, generation, active) VALUES ('asgn_1', 'slot-home', ?, 0, 1, 1)", (old_qid,))
        await conn.execute("INSERT INTO query_membership (query_id, avito_id, first_seen, last_seen, baseline_epoch) VALUES (?, 'item_999', '2026-11-11', '2026-11-11', 0)", (old_qid,))
    await writer.submit(_seed)

    reconciled = await reconcile_conflicting_f_queries(writer)
    assert reconciled == [(old_qid, "q_ed8e9abe6de70ba1")]
    new_qid = reconciled[0][1]

    async def _verify(conn):
        assert tuple(await (await conn.execute("SELECT query_id, max_price FROM subscriptions WHERE id = 'sub_1'")).fetchone()) == (new_qid, 30000)
        assert tuple(await (await conn.execute("SELECT query_id, active FROM assignments WHERE id = 'asgn_1'")).fetchone()) == (new_qid, 1)
        new_q = (await (await conn.execute("SELECT canonical_url FROM queries WHERE id = ?", (new_qid,))).fetchone())[0]
        assert "pmax=30000" in new_q and extract_f_price_ceiling(new_q.split("f=")[1].split("&")[0]) == 30000
        assert await (await conn.execute("SELECT canonical_url FROM queries WHERE id = ?", (old_qid,))).fetchone() is None
        assert [r[0] for r in await (await conn.execute("SELECT avito_id FROM query_membership WHERE query_id = ?", (new_qid,))).fetchall()] == ["item_999"]
    await writer.submit(_verify)
    assert await reconcile_conflicting_f_queries(writer) == []

@pytest.mark.asyncio
async def test_reconcile_leaves_non_conflicting_and_plain_queries_untouched(storage):
    writer = storage
    async def _seed(conn):
        await conn.execute("INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES ('q_p', 'https://www.avito.ru?pmax=4000', 'ready'), ('q_m', 'https://www.avito.ru?f=" + F_100K + "&pmax=100000', 'ready')")
        await conn.execute("INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords, name, enabled, version) VALUES ('s1', 1, 'q_p', 4000, '', '', 1, 1), ('s2', 1, 'q_m', 100000, '', '', 1, 1)")
    await writer.submit(_seed)
    assert await reconcile_conflicting_f_queries(writer) == []
