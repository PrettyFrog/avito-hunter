"""Public-v2 destination mutations must use the shared SQLite writer actor."""

from __future__ import annotations

import re
from pathlib import Path

import aiosqlite
import pytest

from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.storage.deliveries import ClaimedDelivery


class FakeSender:
    async def __call__(self, delivery: ClaimedDelivery) -> SendResult:
        return SendResult("sent", "unused")


def test_legacy_database_modules_do_not_mutate_v2_owned_tables() -> None:
    repo_root = Path(__file__).parents[2]
    legacy_files = [repo_root / "src/hunter/core/db.py"]
    legacy_files.extend((repo_root / "src/hunter/core/db_parts").glob("*.py"))
    v2_tables = {
        "queries", "subscriptions", "query_membership", "subscription_seen",
        "listing_state", "events", "recipient_events", "deliveries", "slots",
        "assignments", "session_versions", "query_groups", "group_members",
        "binding_consumed_nonces", "price_watch_jobs", "subscription_tombstones",
    }
    write = re.compile(
        r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)",
        re.IGNORECASE,
    )
    violations = []
    for path in legacy_files:
        for table in write.findall(path.read_text(encoding="utf-8")):
            if table.lower() in v2_tables:
                violations.append(f"{path.relative_to(repo_root)} writes {table}")
    assert violations == []


def test_direct_connection_helpers_have_no_unqueued_production_callers() -> None:
    repo_root = Path(__file__).parents[2]
    source = repo_root / "src/hunter"
    calls = {
        "channel binding revoke": 0,
        "VK fallback delivery": 0,
    }
    for path in source.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name != "channel_binding.py":
            calls["channel binding revoke"] += text.count(".block_or_revoke_destination(")
        if path.name != "vk_delivery.py":
            calls["VK fallback delivery"] += text.count("create_vk_fallback_delivery(")
    assert calls == {"channel binding revoke": 0, "VK fallback delivery": 0}

    listener = (source / "application/vk_binding_listener.py").read_text(encoding="utf-8")
    assert "self.writer.submit(" in listener
    assert "self.binding_service.bind_from_peer(conn, peer_id, token)" in listener


@pytest.mark.asyncio
async def test_vk_901_destination_revoke_runs_through_writer_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "writer-boundary.db"),
                    senders={"telegram": FakeSender()})
    await app.startup()
    try:
        user_id = await app.db.create_user(telegram_user_id=8181, chat_id=8181)

        async def seed(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "INSERT INTO channel_destinations "
                "(user_id, channel, address, verified) VALUES (?, 'vk', '991', 1)",
                (user_id,),
            )
        await app.writer.submit(seed)

        # Keep the delivery poller's periodic claim from sharing this assertion.
        assert app.dispatcher is not None
        await app.dispatcher.stop()

        writes: list[str] = []
        original_submit = app.writer.submit

        async def count_submit(command, **kwargs):
            writes.append("actor")
            return await original_submit(command, **kwargs)
        monkeypatch.setattr(app.writer, "submit", count_submit)

        from hunter.notify.vk_parts.vknotifier_send_notification_01 import handle_vk_send_error
        await handle_vk_send_error(
            {"error_code": 901, "error_msg": "private recipient unavailable"},
            effective_uid=991, recipient_uid=user_id, db=app.db, telegram_notifier=None,
        )

        row = await (await app.db._conn.execute(
            "SELECT verified FROM channel_destinations WHERE user_id = ? AND channel = 'vk'",
            (user_id,),
        )).fetchone()
        assert row is not None and int(row[0]) == 0
        assert writes == ["actor"]
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_public_quiet_settings_use_writer_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "quiet-writer.db"),
                    senders={"telegram": FakeSender()})
    await app.startup()
    try:
        user_id = await app.db.create_user(telegram_user_id=8182, chat_id=8182)
        await app.dispatcher.stop()
        original = app.writer.submit
        calls = []

        async def counting(command, **kwargs):
            calls.append("writer")
            return await original(command, **kwargs)

        monkeypatch.setattr(app.writer, "submit", counting)
        await app.db.update_user_quiet_hours(user_id, 23, 8)
        assert calls == ["writer"]

        async def failed(*_args, **_kwargs):
            raise OSError("writer unavailable")

        monkeypatch.setattr(app.writer, "submit", failed)
        with pytest.raises(OSError, match="writer unavailable"):
            await app.db.update_user_quiet_hours(user_id, None, None)
        row = await (await app.db._conn.execute(
            "SELECT quiet_start, quiet_end FROM users WHERE id=?", (user_id,)
        )).fetchone()
        assert tuple(row) == (23, 8)
    finally:
        await app.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["block_user_by_owner", "unblock_user_by_owner", "mark_user_bot_blocked"])
async def test_public_user_status_changes_use_writer_and_fail_closed(tmp_path, monkeypatch, action):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "status-writer.db"),
                    senders={"telegram": FakeSender()})
    await app.startup()
    try:
        user_id = await app.db.create_user(telegram_user_id=8183, chat_id=8183)
        await app.dispatcher.stop()
        original = app.writer.submit
        calls = []

        async def counting(command, **kwargs):
            calls.append("writer")
            return await original(command, **kwargs)

        monkeypatch.setattr(app.writer, "submit", counting)
        await getattr(app.db, action)(user_id)
        assert calls == ["writer"]
        row = await (await app.db._conn.execute(
            "SELECT status FROM users WHERE id=?", (user_id,)
        )).fetchone()
        before = row[0]

        async def failed(*_args, **_kwargs):
            raise OSError("writer unavailable")

        monkeypatch.setattr(app.writer, "submit", failed)
        with pytest.raises(OSError, match="writer unavailable"):
            await getattr(app.db, action)(user_id)
        row = await (await app.db._conn.execute(
            "SELECT status FROM users WHERE id=?", (user_id,)
        )).fetchone()
        assert row[0] == before
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_public_status_hierarchy_and_deleted_user_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "hierarchy.db"),
                    senders={"telegram": FakeSender()})
    await app.startup()
    try:
        user_id = await app.db.create_user(telegram_user_id=8184, chat_id=8184)
        await app.dispatcher.stop()
        await app.db.block_user_by_owner(user_id)
        await app.db.mark_user_bot_blocked(user_id)
        row = await (await app.db._conn.execute(
            "SELECT status FROM users WHERE id=?", (user_id,)
        )).fetchone()
        assert row[0] == "blocked_by_owner"
        await app.db.delete_user_data(user_id)
        await app.db.unblock_user_by_owner(user_id)
        row = await (await app.db._conn.execute(
            "SELECT status FROM users WHERE id=?", (user_id,)
        )).fetchone()
        assert row[0] == "deleted"
    finally:
        await app.shutdown()


@pytest.mark.asyncio
async def test_public_user_limits_use_writer_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    app = Bootstrap(settings, db=Database(tmp_path / "limits-writer.db"),
                    senders={"telegram": FakeSender()})
    await app.startup()
    try:
        user_id = await app.db.create_user(telegram_user_id=8185, chat_id=8185)
        await app.dispatcher.stop()
        original = app.writer.submit
        calls = []

        async def counting(command, **kwargs):
            calls.append("writer")
            return await original(command, **kwargs)

        monkeypatch.setattr(app.writer, "submit", counting)
        await app.db.update_user_limits(user_id, max_products=7, min_interval_minutes=30)
        assert calls == ["writer"]
        await app.db.update_user_limits(user_id, max_products=9)
        assert calls == ["writer", "writer"]

        async def failed(*_args, **_kwargs):
            raise OSError("writer unavailable")

        monkeypatch.setattr(app.writer, "submit", failed)
        with pytest.raises(OSError, match="writer unavailable"):
            await app.db.update_user_limits(user_id, max_products=8)
        row = await (await app.db._conn.execute(
            "SELECT max_products, min_interval_minutes FROM users WHERE id=?", (user_id,)
        )).fetchone()
        assert tuple(row) == (9, 30)
    finally:
        await app.shutdown()
