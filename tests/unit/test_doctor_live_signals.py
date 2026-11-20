from pathlib import Path

import pytest

from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.system.doctor import run_diagnostics


@pytest.mark.asyncio
async def test_public_doctor_uses_real_slot_health_and_marks_unknowns(tmp_path: Path):
    settings = Settings(env="test", data_dir=tmp_path, db_path=tmp_path / "db.sqlite")
    settings.public_v2.enabled = True
    db = Database(tmp_path / "db.sqlite")
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO slots (id, endpoint, cookie_namespace, health_state) "
        "VALUES ('slot', 'http://proxy:8080', 'slot', 'QUARANTINED')"
    )
    await conn.execute(
        "INSERT INTO events (id, avito_id, revision, kind, observed_at) "
        "VALUES ('event', 'listing', 1, 'NEW_LISTING', '2020-01-01T00:00:00Z')"
    )
    await conn.execute(
        "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, created_at) "
        "VALUES ('delivery', 'event', 1, 'telegram', 'leased', "
        "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
    )
    await conn.commit()
    try:
        report = await run_diagnostics(db=db, settings=settings)
        runtime = next(check for check in report.checks if check.name == "public_runtime")
        assert runtime.status == "warn"
        assert "quarantined_slots" in runtime.details["reason_codes"]
        assert "relay_unknown" in runtime.details["reason_codes"]
        assert "poll_window_status_unknown" in runtime.details["reason_codes"]
        assert "outbox_oldest_age" in runtime.details["reason_codes"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_public_doctor_without_storage_is_not_healthy(tmp_path: Path):
    settings = Settings(env="test", data_dir=tmp_path, db_path=tmp_path / "db.sqlite")
    settings.public_v2.enabled = True
    report = await run_diagnostics(settings=settings)
    runtime = next(check for check in report.checks if check.name == "public_runtime")
    assert runtime.status == "fail"
    assert "runtime_storage_unavailable" in runtime.details["reason_codes"]
    assert not report.is_healthy
