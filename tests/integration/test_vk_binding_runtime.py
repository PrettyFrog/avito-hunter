"""Fake VK Long Poll event binds through Bootstrap's single SQLite writer."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hunter.application.channel_binding import ChannelBindingService
from hunter.application.vk_binding_listener import VkBindingListener
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.transport.vk_longpoll import VkLongPollClient, VkPollBatch


@pytest.mark.asyncio
async def test_private_vk_message_binds_tenant_and_replay_is_ignored(tmp_path: Path) -> None:
    settings = _binding_settings()
    db = Database(tmp_path / "vk-binding.db")
    await db.connect()
    user_id = await db.create_user(
        telegram_user_id=8801, chat_id=8801, role="user", status="active"
    )
    bootstrap = Bootstrap(settings, db=db, senders={"telegram": lambda *_: None})
    await bootstrap.startup()
    token = bootstrap.channel_binding_service.generate_token(user_id)
    client = _vk_client(settings, token)
    try:
        assert (
            bootstrap.subscription_service.channel_binding_service
            is bootstrap.channel_binding_service
        )
        listener = VkBindingListener(
            42, bootstrap.writer, bootstrap.channel_binding_service, client
        )
        assert await listener.poll_once() == 1
        assert await listener.handle_update(_binding_event(token)) is False
        await _assert_binding_rows(db, user_id)
        assert await listener.handle_update(_binding_event(token, peer_id=2_000_000_001)) is False
    finally:
        await client.close()
        await bootstrap.shutdown()


def _binding_settings() -> Settings:
    return Settings(
        _env_file=None,
        public_v2={"enabled": True},
        telegram_bot_token="fake-telegram-token",
        telegram_chat_id="999",
        vk_binding_enabled=True,
        vk_group_id=42,
        vk_group_token="fake-vk-group-token",
        channel_binding_secret="private-binding-secret-1234567890",
    )


def _vk_client(settings: Settings, token: str) -> VkLongPollClient:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = {"response": {"server": "https://lp.vk.com/longpoll", "key": "fake", "ts": "1"}}
        else:
            body = {"ts": "2", "updates": [_binding_event(f"bind {token}")]}
        return httpx.Response(200, json=body, request=request)

    return VkLongPollClient(
        settings.vk_group_id,
        settings.vk_group_token.get_secret_value(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


def _binding_event(text: str, peer_id: int = 771) -> dict:
    return {
        "type": "message_new",
        "group_id": 42,
        "object": {"message": {"from_id": 771, "peer_id": peer_id, "text": text}},
    }


async def _assert_binding_rows(db: Database, user_id: int) -> None:
    row = await (
        await db._conn.execute(
            "SELECT address, verified FROM channel_destinations WHERE user_id=? AND channel='vk'",
            (user_id,),
        )
    ).fetchone()
    assert tuple(row) == ("771", 1)
    count = await (
        await db._conn.execute("SELECT COUNT(*) FROM binding_consumed_nonces")
    ).fetchone()
    assert count[0] == 1


@pytest.mark.asyncio
async def test_writer_failure_does_not_ack_the_longpoll_batch() -> None:
    service = ChannelBindingService("private-binding-secret-1234567890")
    token = service.generate_token(1)
    update = {
        "type": "message_new",
        "group_id": 42,
        "object": {"message": {"from_id": 771, "peer_id": 771, "text": token}},
    }

    class _Client:
        acked = False

        async def poll(self) -> VkPollBatch:
            return VkPollBatch((update,), "5")

        def ack(self, _batch: VkPollBatch) -> None:
            self.acked = True

    class _Writer:
        async def submit(self, _command):
            raise OSError("temporary database fault")

    client = _Client()
    listener = VkBindingListener(42, _Writer(), service, client)
    with pytest.raises(OSError, match="temporary database fault"):
        await listener.poll_once()
    assert not client.acked


@pytest.mark.asyncio
async def test_malformed_group_id_is_ignored_without_writer_effect() -> None:
    class _Writer:
        async def submit(self, _command):
            raise AssertionError("malformed events must not reach storage")

    update = _binding_event("bnd_1_malformed")
    update["group_id"] = "invalid"
    listener = VkBindingListener(42, _Writer(), object(), object())
    assert await listener.handle_update(update) is False
