"""Binding token issuance and consumption survive process restarts."""

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from hunter.application.channel_binding import ChannelBindingService, TokenReplayError
from hunter.core.config import Settings
from hunter.core.db import Database


def test_binding_requires_private_installation_secret_and_unique_nonce() -> None:
    with pytest.raises((TypeError, ValueError)):
        ChannelBindingService()
    with pytest.raises(ValueError):
        ChannelBindingService("short")
    service = ChannelBindingService("x" * 32)
    assert service.generate_token(101) != service.generate_token(101)


def test_secret_is_loaded_from_masked_settings() -> None:
    secret = "test-private-channel-binding-secret-2026"
    settings = Settings(_env_file=None, channel_binding_secret=secret)
    assert secret not in repr(settings)
    assert ChannelBindingService.from_settings(settings).generate_token(101).startswith("bnd_101_")


@pytest.mark.asyncio
async def test_replay_rejected_after_service_restart(tmp_path: Path) -> None:
    path = tmp_path / "binding.db"
    db = Database(path)
    await db.connect()
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO users (id, role, created_at) VALUES (101, 'user', datetime('now'))"
    )
    await conn.commit()
    token = ChannelBindingService("x" * 32).generate_token(101)
    assert await ChannelBindingService("x" * 32).bind_channel(conn, 101, "vk", "111", token)
    await db.close()
    restarted = Database(path)
    await restarted.connect()
    conn = await restarted.connect()
    try:
        with pytest.raises(TokenReplayError):
            await ChannelBindingService("x" * 32).bind_channel(conn, 101, "vk", "222", token)
        row = await (
            await conn.execute(
                "SELECT address FROM channel_destinations WHERE user_id=101 AND channel='vk'"
            )
        ).fetchone()
        assert row[0] == "111"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_concurrent_consume_allows_one_binding(tmp_path: Path) -> None:
    path = tmp_path / "binding.db"
    db = Database(path)
    await db.connect()
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO users (id, role, created_at) VALUES (101, 'user', datetime('now'))"
    )
    await conn.commit()
    token = ChannelBindingService("x" * 32).generate_token(101)
    try:
        async with aiosqlite.connect(path) as left, aiosqlite.connect(path) as right:
            attempts = await asyncio.gather(
                ChannelBindingService("x" * 32).bind_channel(left, 101, "vk", "111", token),
                ChannelBindingService("x" * 32).bind_channel(right, 101, "vk", "222", token),
                return_exceptions=True,
            )
        assert sum(result is True for result in attempts) == 1
        assert sum(isinstance(result, TokenReplayError) for result in attempts) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_bind_rolls_back_nonce_consumption(tmp_path: Path) -> None:
    db = Database(tmp_path / "failed.db")
    await db.connect()
    conn = await db.connect()
    service = ChannelBindingService("x" * 32)
    token = service.generate_token(101)
    try:
        await conn.execute(
            "INSERT INTO users (id, role, created_at) VALUES (101, 'user', datetime('now'))"
        )
        await conn.execute(
            "CREATE TRIGGER reject_destination BEFORE INSERT ON channel_destinations "
            "BEGIN SELECT RAISE(ABORT, 'injected fault'); END"
        )
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await service.bind_channel(conn, 101, "vk", "111", token)
        await conn.execute("DROP TRIGGER reject_destination")
        await conn.commit()
        assert await service.bind_channel(conn, 101, "vk", "111", token)
    finally:
        await db.close()
