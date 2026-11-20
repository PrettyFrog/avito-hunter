from unittest.mock import AsyncMock

import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.slots import SlotHealth


@pytest.mark.asyncio
async def test_health_estimates_actual_pollable_assignments(tmp_path):
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.day_interval_sec = 60
    settings.public_v2.night_interval_sec = 300
    bootstrap = Bootstrap(
        settings, db=Database(tmp_path / "test.db"), senders={"telegram": AsyncMock()}
    )
    await bootstrap.startup()
    try:
        assert (await bootstrap.get_health())["search_workload_requests_per_day"] == 0
        await bootstrap.slot_repo.register_slot("slot_a", "http://proxy:8080")
        await bootstrap.slot_repo.register_slot(
            "slot_b", "http://proxy:8081", health=SlotHealth.DISABLED
        )

        async def insert(conn):
            for i, slot in enumerate(("slot_a", "slot_b")):
                await conn.execute(
                    "INSERT INTO queries (id, canonical_url) VALUES (?, ?)",
                    (f"q{i}", f"https://avito.ru/?q={i}"),
                )
                await conn.execute(
                    "INSERT INTO assignments (id, slot_id, query_id, position, generation) "
                    "VALUES (?, ?, ?, 0, 1)",
                    (f"a{i}", slot, f"q{i}"),
                )

        await bootstrap.writer.submit(insert)
        assert (await bootstrap.get_health())["search_workload_requests_per_day"] == 1152
    finally:
        await bootstrap.shutdown()
