import aiosqlite
import pytest

from hunter.storage.migrations.v012_subscriptions import V012_SQL
from hunter.storage.migrations.v014_slots import V014_SQL
from hunter.storage.read_models import fetch_query_economics, redact_proxy_endpoint


@pytest.mark.asyncio
async def test_economics_reads_real_slot_state_without_proxy_credentials():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(V012_SQL + V014_SQL)
        await conn.execute("ALTER TABLE slots ADD COLUMN role TEXT NOT NULL DEFAULT 'active'")
        await conn.execute(
            "INSERT INTO queries (id, canonical_url) VALUES (?, ?)",
            ("q1", "https://www.avito.ru/x"),
        )
        await conn.execute(
            "INSERT INTO subscriptions (id, user_id, query_id, max_price) VALUES (?, ?, ?, ?)",
            ("s1", 1, "q1", 100),
        )
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state) VALUES (?, ?, ?, ?)",
            ("slot1", "http://alice:password@proxy.example:8080?token=secret", "s1", "QUARANTINED"),
        )
        model = await fetch_query_economics(conn, is_owner=True)
        assert (model.logical_subscriptions, model.physical_queries, model.quarantine_slots) == (
            1, 1, 1
        )
        assert model.ip_pool_details is not None
        endpoint = model.ip_pool_details[0]["endpoint"]
        assert "proxy.example" in endpoint
        assert not any(secret in endpoint for secret in ("alice", "password", "secret"))
        public = await fetch_query_economics(conn, is_owner=False)
        assert public.ip_pool_details is None


@pytest.mark.parametrize("endpoint", ["http://secret@[invalid", "not-a-url?token=secret"])
def test_malformed_proxy_endpoint_never_echoes_input(endpoint):
    assert redact_proxy_endpoint(endpoint) == "[redacted]"


@pytest.mark.asyncio
async def test_economics_fails_when_required_schema_is_missing():
    async with aiosqlite.connect(":memory:") as conn:
        with pytest.raises(aiosqlite.OperationalError):
            await fetch_query_economics(conn)
