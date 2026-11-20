"""Tests for VK channel binding and recipient verification (S6 / T-510)."""

from __future__ import annotations

import hashlib
import hmac
import time
import pytest
import aiosqlite
import httpx

from hunter.application.channel_binding import (
    ChannelBindingService,
    BindingError,
    ForgedTokenError,
    ExpiredTokenError,
    TokenReplayError,
    CrossUserBindingError,
)
from hunter.core.models import NotificationEvent
from hunter.notify.vk import VkNotifier

TEST_SECRET = "test_secret_for_channel_binding_2026"


@pytest.fixture
async def db_conn():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys = ON;")
    await conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE channel_destinations (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            channel TEXT NOT NULL CHECK(channel IN ('telegram', 'vk')),
            address TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, channel)
        );
        CREATE TABLE binding_consumed_nonces (
            nonce TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            channel TEXT NOT NULL, consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users (id) VALUES (1), (2), (3);
    """)
    await conn.commit()
    yield conn
    await conn.close()


def make_test_event(user_id: int | None = None) -> NotificationEvent:
    return NotificationEvent(
        avito_id="12345",
        url="https://avito.ru/1",
        title="M50x",
        price=10000,
        max_price=12000,
        watchlist_id="w1",
        category="audio",
        model_id="m1",
        verdict="GOOD",
        reasons=["ok"],
        recipient_user_id=user_id,
    )


@pytest.mark.asyncio
async def test_token_generation_and_valid_binding(db_conn):
    svc = ChannelBindingService(TEST_SECRET)
    token = svc.generate_token(user_id=1, channel="vk")
    assert token.startswith("bnd_1_")
    assert await svc.bind_channel(db_conn, 1, "vk", "123456", token) is True
    assert await svc.is_destination_verified(db_conn, 1, "vk") is True


@pytest.mark.asyncio
async def test_bind_from_peer_and_arbitrary_peer_rejection(db_conn):
    svc = ChannelBindingService(TEST_SECRET)
    token = svc.generate_token(user_id=2, channel="vk")
    with pytest.raises(BindingError, match="Arbitrary or non-user peer rejected"):
        await svc.bind_from_peer(db_conn, peer_id=2000000042, token=token)
    with pytest.raises(BindingError, match="Arbitrary or non-user peer rejected"):
        await svc.bind_from_peer(db_conn, peer_id=-10, token=token)
    assert (
        await svc.bind_from_peer(db_conn, peer_id=777, token=token, expected_actor_user_id=2)
        is True
    )
    assert await svc.is_destination_verified(db_conn, 2, "vk") is True


@pytest.mark.asyncio
async def test_forged_expired_replay_and_cross_user_rejection(db_conn):
    svc = ChannelBindingService(TEST_SECRET)
    t1 = svc.generate_token(user_id=1)
    with pytest.raises(ForgedTokenError):
        await svc.bind_channel(db_conn, 1, "vk", "123", t1[:-3] + "xxx")
    with pytest.raises(CrossUserBindingError):
        await svc.bind_channel(db_conn, 2, "vk", "123", t1)

    past = int(time.time()) - 1000
    salt = hashlib.sha256(f"{past}:1".encode()).hexdigest()[:8]
    sig = hmac.new(
        TEST_SECRET.encode(), f"1:vk:{past}:{salt}".encode(), hashlib.sha256
    ).hexdigest()[:16]
    with pytest.raises(ExpiredTokenError):
        svc.verify_token(f"bnd_1_{past}_{salt}_{sig}", expected_user_id=1, ttl_seconds=600)

    await svc.bind_channel(db_conn, 1, "vk", "123", t1)
    with pytest.raises(TokenReplayError):
        await svc.bind_channel(db_conn, 1, "vk", "123", t1)


@pytest.mark.asyncio
async def test_isolated_revocation_and_blocking(db_conn):
    svc = ChannelBindingService(TEST_SECRET)
    await svc.bind_channel(db_conn, 1, "vk", "111", svc.generate_token(1))
    await svc.bind_channel(db_conn, 2, "vk", "222", svc.generate_token(2))
    assert await svc.block_or_revoke_destination(db_conn, 1) is True
    assert await svc.is_destination_verified(db_conn, 1) is False
    assert await svc.is_destination_verified(db_conn, 2) is True


def test_secrets_not_leaked():
    svc = ChannelBindingService("super_secret_vk_key_999" * 2)
    t = svc.generate_token(1)
    with pytest.raises(ForgedTokenError) as exc1:
        svc.verify_token(t[:-4] + "0000", expected_user_id=1)
    assert "super_secret_vk_key_999" not in str(exc1.value)
    with pytest.raises(CrossUserBindingError) as exc2:
        svc.verify_token(t, expected_user_id=99)
    assert "super_secret_vk_key_999" not in str(exc2.value)


@pytest.mark.asyncio
async def test_legacy_owner_destination_not_fallback_for_other_users(db_conn):
    notifier = VkNotifier(token="mock_vk_token", user_id=999999)
    assert await notifier.send_notification(make_test_event(user_id=2), db=db_conn) is False

    mock_c = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"response": 1}))
    )
    async with mock_c as client:
        assert (
            await notifier.send_notification(
                make_test_event(user_id=None), client=client, db=db_conn
            )
            is True
        )


@pytest.mark.asyncio
async def test_vk_error_901_revokes_destination_for_user(db_conn):
    svc = ChannelBindingService(TEST_SECRET)
    await svc.bind_channel(db_conn, 3, "vk", "333", svc.generate_token(3))
    notifier = VkNotifier(token="mock_vk_token", user_id=999999)
    mock_c = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"error": {"error_code": 901, "error_msg": "Blocked"}}
            )
        )
    )
    async with mock_c as client:
        assert (
            await notifier.send_notification(make_test_event(user_id=3), client=client, db=db_conn)
            is False
        )
    assert await svc.is_destination_verified(db_conn, 3) is False
