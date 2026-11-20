from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.slots import SlotRole
from hunter.storage.migration_runner import LATEST_VERSION, MigrationRunner


@pytest.mark.asyncio
async def test_active_slot_with_res_substring_keeps_explicit_role_and_accepts_query(
    tmp_path: Path,
) -> None:
    settings = Settings(env="test", data_dir=tmp_path)
    settings.public_v2.enabled = True
    db = Database(tmp_path / "roles.db")
    bootstrap = Bootstrap(settings, db=db, senders={"telegram": AsyncMock()})
    await bootstrap.startup()
    try:
        slot = await bootstrap.slot_repo.register_slot(
            "forest", "http://slot.invalid:8080", role=SlotRole.ACTIVE
        )
        assert slot.role == SlotRole.ACTIVE
        spare = await bootstrap.slot_repo.register_slot(
            "spare_01", "http://reserve.invalid:8080", role=SlotRole.RESERVE
        )
        assert spare.role == SlotRole.RESERVE
        users = await bootstrap.db.get_all_users()
        used_ids = {int(u["telegram_user_id"]) for u in users if u.get("telegram_user_id")}
        telegram_id = 987654321
        while telegram_id in used_ids:
            telegram_id += 1
        user_id = await bootstrap.db.create_user(
            telegram_user_id=telegram_id, chat_id=telegram_id, role="user", status="active"
        )
        sub = await bootstrap.subscription_service.add_subscription(
            user_id, "https://www.avito.ru/moskva?q=phone", 10_000
        )
        row = await (
            await bootstrap.db._conn.execute(
                "SELECT slot_id FROM assignments WHERE query_id = ? AND active = 1",
                (sub.query_id,),
            )
        ).fetchone()
        assert row[0] == "forest"
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_v18_migration_backfills_legacy_reserve_role_once(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v17.db"
    db = Database(path)
    await db.connect()
    await db.migrate()
    await db.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace) VALUES ('res_01', 'http://r', 'r')"
        )
        conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace) "
            "VALUES ('active_01', 'http://a', 'a')"
        )
        conn.execute("ALTER TABLE slots DROP COLUMN role")
        conn.execute("DELETE FROM schema_migrations WHERE version = 18")
        conn.execute("PRAGMA user_version = 17")
        conn.commit()

        assert await MigrationRunner(path).run_migrations(conn) == LATEST_VERSION
        roles = dict(conn.execute("SELECT id, role FROM slots").fetchall())
        assert roles == {"res_01": "reserve", "active_01": "active"}
    finally:
        conn.close()
