import asyncio
from unittest.mock import AsyncMock

import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database


@pytest.mark.asyncio
async def test_bootstrap_runs_monitor_and_cancels_it_on_shutdown(tmp_path):
    settings = Settings(env="test", data_dir=tmp_path, db_path=tmp_path / "db.sqlite")
    settings.public_v2.enabled = True
    db = Database(tmp_path / "db.sqlite")
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO slots (id, endpoint, cookie_namespace, health_state) "
        "VALUES ('slot', 'http://proxy:8080', 'slot', 'QUARANTINED')"
    )
    await conn.commit()
    delivered = asyncio.Event()

    async def fake_alert(message):
        assert "quarantined_slots" in message
        delivered.set()
        return True

    bootstrap = Bootstrap(settings, db=db, senders={"telegram": AsyncMock()})
    bootstrap.owner_alert_sender = fake_alert
    await bootstrap.startup()
    try:
        await asyncio.wait_for(delivered.wait(), timeout=2)
        task = next(
            task for task in bootstrap._tasks if task.get_name() == "runtime_health_monitor"
        )
    finally:
        await bootstrap.shutdown()
    assert task.done()


@pytest.mark.asyncio
async def test_monitor_read_does_not_join_uncommitted_writer_transaction(tmp_path):
    from hunter.system.runtime_health import read_runtime_health

    db = Database(tmp_path / "isolated.sqlite")
    conn = await db.connect()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state) "
            "VALUES ('slot', 'http://proxy:8080', 'slot', 'QUARANTINED')"
        )
        snapshot = await read_runtime_health(db)
        assert snapshot.quarantined_slots == 0
        assert conn.in_transaction
        await conn.commit()
        assert (await read_runtime_health(db)).quarantined_slots == 1
    finally:
        await db.close()
