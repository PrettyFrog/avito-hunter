"""Unit tests for typed VK delivery adapter (T-487)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest

from hunter.notify.vk_delivery import (
    VkDeliveryAdapter,
    VkDeliveryEnvelope,
    create_vk_fallback_delivery,
    format_vk_delivery_text,
    get_stable_random_id,
)
from hunter.storage.deliveries import ClaimedDelivery

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.fixture
async def db_conn(tmp_path: Path):
    db_path = tmp_path / "vk_del_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()
    async with aiosqlite.connect(db_path) as c:
        await c.execute("PRAGMA foreign_keys = ON")
        yield c


@pytest.mark.asyncio
async def test_vk_delivery_success_and_acceptance_id() -> None:
    client, resp = AsyncMock(), MagicMock()
    resp.json.return_value = {"response": 998877}
    client.post.return_value = resp
    adapter = VkDeliveryAdapter(token="tok_test", client=client)
    c = ClaimedDelivery(
        "del_1",
        "ev_1",
        101,
        "vk",
        1,
        {"title": "Item", "price": 5000, "url": "https://a.ru/1"},
        1,
        "tok",
        "t",
    )
    res = await adapter.send_delivery(c, destination=123456)
    assert res.status == "sent" and res.message_id == "998877"
    assert client.post.call_args.kwargs["data"]["user_id"] == 123456


@pytest.mark.asyncio
async def test_vk_error_classification() -> None:
    adapter = VkDeliveryAdapter(token="tok_test", client=AsyncMock())
    env = VkDeliveryEnvelope(destination=123, text="Hi", delivery_id="del_err")

    adapter.client.post.return_value = MagicMock(
        json=lambda: {"error": {"error_code": 901, "error_msg": "Blocked"}}
    )
    assert (await adapter.send_envelope(env)).status == "blocked"

    adapter.client.post.return_value = MagicMock(
        json=lambda: {"error": {"error_code": 9, "error_msg": "Flood"}}
    )
    assert (await adapter.send_envelope(env)).retry_after == 60

    adapter.client.post.side_effect = httpx.ReadTimeout("Timeout")
    assert (await adapter.send_envelope(env)).status == "ambiguous"


def test_stable_random_id() -> None:
    assert (
        get_stable_random_id("d_10") == get_stable_random_id("d_10") != get_stable_random_id("d_20")
    )
    assert 0 <= get_stable_random_id("d_10") <= 0x7FFFFFFF


@pytest.mark.asyncio
async def test_create_vk_fallback_delivery_with_durable_delay(
    db_conn: aiosqlite.Connection,
) -> None:
    now_ts = datetime.now(UTC).isoformat()
    await db_conn.execute(
        "INSERT INTO users (id, role, status, created_at, last_seen_at) "
        "VALUES (101, 'user', 'active', ?, ?)",
        (now_ts, now_ts),
    )
    await db_conn.execute(
        "INSERT INTO channel_destinations (user_id, channel, address, verified) "
        "VALUES (101, 'vk', '777888', 1)"
    )
    await db_conn.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at) "
        "VALUES ('ev_1', 'lot_1', 1, 'NEW_LISTING', ?)",
        (now_ts,),
    )
    await db_conn.commit()

    c_tg = ClaimedDelivery(
        "del_ev_1_101_tg",
        "ev_1",
        101,
        "telegram",
        1,
        {"title": "Mac", "price": 40000},
        1,
        "tok",
        "t",
    )
    vk_id = await create_vk_fallback_delivery(db_conn, c_tg, delay_seconds=600)
    assert vk_id == "del_ev_1_101_vk"
    row = await (
        await db_conn.execute(
            "SELECT channel, event_id, status, attempt FROM deliveries WHERE id = ?", (vk_id,)
        )
    ).fetchone()
    assert row == ("vk", "ev_1", "pending", 0)
    assert (
        await create_vk_fallback_delivery(
            db_conn, ClaimedDelivery("d_bad", "ev_1", 999, "telegram", 1, {}, 1, "t", "t")
        )
        is None
    )


@pytest.mark.asyncio
async def test_vk_fallback_uses_verified_current_destination_version(db_conn):
    await db_conn.execute(
        "INSERT INTO users (id, role, status, created_at, last_seen_at) "
        "VALUES (101, 'user', 'active', '2026-10-23T12:00:00Z', '2026-10-23T12:00:00Z')"
    )
    await db_conn.execute(
        "INSERT INTO channel_destinations (user_id, channel, address, verified, version) "
        "VALUES (101, 'vk', '777888', 1, 3)"
    )
    await db_conn.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at) "
        "VALUES ('event_v3', 'listing', 1, 'NEW_LISTING', '2026-10-23T12:00:00Z')"
    )
    delivery = ClaimedDelivery(
        "telegram", "event_v3", 101, "telegram", 1, {"title": "Item"}, 1, "token", "now"
    )
    fallback_id = await create_vk_fallback_delivery(db_conn, delivery, delay_seconds=0)
    row = await (
        await db_conn.execute(
            "SELECT destination_version FROM deliveries WHERE id = ?", (fallback_id,)
        )
    ).fetchone()
    assert row[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"response": None}, {"response": 0}])
async def test_vk_missing_acceptance_id_is_not_success(body):
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = VkDeliveryAdapter(token="fake", client=client)
        result = await adapter.send_envelope(VkDeliveryEnvelope(123, "item", "delivery"))
    assert result.status == "ambiguous"


@pytest.mark.parametrize("old,new", [(100, 120), (0, 100), (120, 100)])
def test_vk_price_change_does_not_claim_every_change_is_a_reduction(old, new):
    text = format_vk_delivery_text({"title": "Item", "old_price": old, "new_price": new})
    assert "Цена изменилась" in text
    assert f"{old} ₽" in text
    assert f"{new} ₽" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [(429, "retry"), (500, "ambiguous")])
async def test_vk_http_error_is_not_a_success_even_with_message_id(status, expected):
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"response": 55}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = VkDeliveryAdapter(token="fake", client=client)
        result = await adapter.send_envelope(VkDeliveryEnvelope(123, "item", "delivery"))
    assert result.status == expected
