"""Public subscriptions atomically reserve a physical polling assignment."""

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.storage.subscriptions import NoActiveSlotCapacityError


async def _assignment_rows(db: Database, query_id: str) -> list[tuple]:
    cur = await db._conn.execute(
        "SELECT slot_id, generation, active FROM assignments WHERE query_id = ? ORDER BY slot_id",
        (query_id,),
    )
    return [tuple(row) for row in await cur.fetchall()]


def _install_fake_http(bootstrap: Bootstrap, slot_id: str, endpoint: str) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="", request=request)

    bootstrap.http_pool.get_fetcher(slot_id, endpoint, transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
async def test_public_add_assigns_once_and_shared_add_reuses_assignment(tmp_path: Path) -> None:
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    sender = AsyncMock(return_value=SendResult("sent", "fake"))
    bootstrap = Bootstrap(
        settings, db=Database(tmp_path / "admission.db"), senders={"telegram": sender}
    )
    await bootstrap.startup()
    try:
        slot = await bootstrap.slot_repo.register_slot("slot_1", "http://slot-1.invalid:8080")
        _install_fake_http(bootstrap, slot.slot_id, slot.endpoint)
        url = "https://www.avito.ru/moskva?q=phone"
        first = await bootstrap.subscription_service.add_subscription(7101, url, 20_000)
        worker = bootstrap.workers[slot.slot_id]
        second = await bootstrap.subscription_service.add_subscription(7102, url, 20_000)
        rows = await _assignment_rows(bootstrap.db, first.query_id)
        assert first.query_id == second.query_id
        assert len(rows) == 1 and rows[0][0] == "slot_1" and rows[0][2] == 1
        assert rows[0][1] > 0
        third = await bootstrap.subscription_service.add_subscription(
            7103, "https://www.avito.ru/moskva?q=tablet", 20_000
        )
        assert bootstrap.workers[slot.slot_id] is worker
        assert worker.assigned_queries == [first.query_id, third.query_id]
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_public_add_without_capacity_rolls_back_subscription(tmp_path: Path) -> None:
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.slot_capacity = 1
    bootstrap = Bootstrap(
        settings, db=Database(tmp_path / "full.db"), senders={"telegram": AsyncMock()}
    )
    await bootstrap.startup()
    try:
        slot = await bootstrap.slot_repo.register_slot("slot_1", "http://slot-1.invalid:8080")
        _install_fake_http(bootstrap, slot.slot_id, slot.endpoint)
        await bootstrap.subscription_service.add_subscription(
            7201, "https://www.avito.ru/moskva?q=phone", 20_000
        )
        with pytest.raises(NoActiveSlotCapacityError):
            await bootstrap.subscription_service.add_subscription(
                7202, "https://www.avito.ru/moskva?q=car", 20_000
            )
        row = await (
            await bootstrap.db._conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = 7202"
            )
        ).fetchone()
        assert int(row[0]) == 0
        queries = await (
            await bootstrap.db._conn.execute("SELECT COUNT(*) FROM queries")
        ).fetchone()
        assert int(queries[0]) == 1
    finally:
        await bootstrap.shutdown()
