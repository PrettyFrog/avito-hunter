"""Tests for price watch lifecycle, composition root wiring, and metrics (S7 / T-509)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from typer.testing import CliRunner

from hunter.bootstrap import Bootstrap
from hunter.cli import app
from hunter.core.config import Settings
from hunter.core.db import Database


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    st = Settings()
    st.public_v2.enabled = True
    st.public_v2.price_watch_enabled = False
    return st


@pytest.mark.asyncio
async def test_price_watch_off_spawns_zero_tasks_and_zero_requests(
    tmp_path: Path, test_settings: Settings
):
    db_p = tmp_path / "hunter_off.db"
    db = Database(db_p)
    bs = Bootstrap(test_settings, db=db, senders={"telegram": AsyncMock()})
    await bs.startup()

    assert bs._price_watch_task is None
    assert bs.price_watch_worker is not None
    assert bs.price_watch_worker.requests_made == 0

    health = await bs.get_health()
    assert health["search_workload_requests_per_day"] == 0
    assert health["price_watch"]["enabled"] is False

    await bs.shutdown()


@pytest.mark.asyncio
async def test_price_watch_on_supervised_lifecycle_and_shutdown_unlease(
    tmp_path: Path, test_settings: Settings
):
    test_settings.public_v2.price_watch_enabled = True
    db_p = tmp_path / "hunter_on.db"
    db = Database(db_p)
    bs = Bootstrap(test_settings, db=db, senders={"telegram": AsyncMock()})
    await bs.startup()

    assert bs._price_watch_task is not None
    assert not bs._price_watch_task.done()

    # Insert a leased job
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO price_watch_jobs (id, item_id, status, next_run_at, lease_token, lease_until) "
        "VALUES ('j_live', '101', 'leased', '2026-10-23T10:00:00+00:00', "
        "'lease_tok', '2026-10-23T10:30:00+00:00')"
    )
    await conn.commit()

    health = await bs.get_health()
    assert health["search_workload_requests_per_day"] == 0
    assert health["price_watch"]["enabled"] is True
    assert health["price_watch"]["extras_overhead"] is True
    assert health["price_watch"]["counts"].get("leased") == 1

    await bs.shutdown()
    assert bs._price_watch_task.done()

    # Recheck DB: lease returned to pending
    conn = await db.connect()
    row = await (
        await conn.execute(
            "SELECT status, lease_token, lease_until FROM price_watch_jobs WHERE id = 'j_live'"
        )
    ).fetchone()
    assert tuple(row) == ("pending", None, None)


def test_cli_price_watch_command(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    res = runner.invoke(app, ["price-watch", "--json"])
    assert res.exit_code == 0
    parsed = json.loads(res.output)
    assert parsed["search_requests_per_day"] == 0
    assert parsed["search_workload_is_estimate"] is True
    assert "enabled" in parsed
    assert "extras_estimate_per_day" in parsed


def test_cli_price_watch_estimates_enabled_assignments(monkeypatch, tmp_path):
    import asyncio
    import hunter.cli as cli

    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.day_interval_sec = 60
    settings.public_v2.night_interval_sec = 300
    db = Database(tmp_path / "cli.db")

    async def seed():
        conn = await db.connect()
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state) "
            "VALUES ('s1', 'http://proxy:8080', 's1', 'READY')"
        )
        await conn.execute(
            "INSERT INTO queries (id, canonical_url) VALUES ('q1', 'https://avito.ru/?q=x')"
        )
        await conn.execute(
            "INSERT INTO assignments (id, slot_id, query_id, position, generation) "
            "VALUES ('a1', 's1', 'q1', 0, 1)"
        )
        await conn.commit()
        await db.close()

    asyncio.run(seed())
    monkeypatch.setattr(cli, "get_db", lambda: db)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    result = CliRunner().invoke(app, ["price-watch", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["search_requests_per_day"] == 1152
    assert report["search_workload_is_estimate"] is True
