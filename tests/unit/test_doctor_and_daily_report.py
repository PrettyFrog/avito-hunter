from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hunter.core.config import Settings
from hunter.core.db import Database


@pytest.mark.asyncio
async def test_doctor_diagnostics_pass_on_clean_env(tmp_path: Path):
    from hunter.system.doctor import run_diagnostics

    db_path = tmp_path / "test_doc.db"
    db = Database(db_path)
    await db.connect()

    # Create dummy backups directory with >= 7 backups
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    for i in range(8):
        (backups_dir / f"backup_20261022_0{i}0000").mkdir()

    settings = Settings(
        db_path=db_path,
        data_dir=tmp_path,
        config_dir=tmp_path / "config",
        raw_dir=tmp_path / "raw",
        gemini_api_key="test_gemini_key",
        gigachat_credentials="test_giga_creds",
    )

    report = await run_diagnostics(db=db, settings=settings, backups_dir=backups_dir)
    assert report.is_healthy is True
    assert len(report.checks) >= 6
    names = [c.name for c in report.checks]
    assert "database_integrity" in names
    assert "backup_retention" in names
    assert "ai_credentials" in names

    await db.close()


@pytest.mark.asyncio
async def test_doctor_detects_backup_retention_warning(tmp_path: Path):
    from hunter.system.doctor import run_diagnostics

    db_path = tmp_path / "test_doc.db"
    db = Database(db_path)
    await db.connect()

    # Only 2 backups (< 7 requirement)
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    for i in range(2):
        (backups_dir / f"backup_20261022_0{i}0000").mkdir()

    settings = Settings(
        db_path=db_path,
        data_dir=tmp_path,
        config_dir=tmp_path / "config",
        raw_dir=tmp_path / "raw",
    )

    report = await run_diagnostics(db=db, settings=settings, backups_dir=backups_dir)
    backup_check = next(c for c in report.checks if c.name == "backup_retention")
    assert backup_check.status in ("warn", "fail")
    assert "2" in backup_check.message

    await db.close()


@pytest.mark.asyncio
async def test_runtime_faults_degrade_health_and_dedupe_owner_alert(tmp_path: Path):
    from hunter.system.doctor import OwnerAlertLimiter, RuntimeHealthSignals, run_diagnostics

    now = [100.0]
    alerts: list[str] = []

    async def send_owner_alert(message: str) -> None:
        alerts.append(message)

    signals = RuntimeHealthSignals(
        relay_status="down",
        relay_http_status=403,
        pending_outbox_count=1001,
        outbox_limit=1000,
        oldest_pending_age_sec=301,
        missed_windows=1,
        quarantined_slots=1,
        wal_bytes=300 * 1024 * 1024,
        rss_bytes=1200 * 1024 * 1024,
    )
    limiter = OwnerAlertLimiter(interval_sec=600, clock=lambda: now[0])
    settings = Settings(
        db_path=tmp_path / "health.db",
        data_dir=tmp_path,
        config_dir=tmp_path / "config",
        raw_dir=tmp_path / "raw",
    )
    first = await run_diagnostics(
        settings=settings,
        runtime_health=signals,
        owner_alert_sender=send_owner_alert,
        alert_limiter=limiter,
    )
    now[0] = 200.0
    second = await run_diagnostics(
        settings=settings,
        runtime_health=signals,
        owner_alert_sender=send_owner_alert,
        alert_limiter=limiter,
    )

    assert first.health_state == second.health_state == "degraded"
    assert not first.is_healthy and not second.is_healthy
    assert len(alerts) == 1
    assert "relay_unavailable" in alerts[0] and "outbox_overflow" in alerts[0]
    assert "missed_poll_windows" in alerts[0] and "quarantined_slots" in alerts[0]
    assert "outbox_oldest_age" in alerts[0] and "wal_size_high" in alerts[0]
    assert "rss_high" in alerts[0]
    assert str(settings.db_path) not in alerts[0]


@pytest.mark.asyncio
async def test_generate_compact_daily_report_file(tmp_path: Path):
    from hunter.system.report import generate_daily_report

    db_path = tmp_path / "test_report.db"
    db = Database(db_path)
    await db.connect()

    # Populate some test data
    now_iso = datetime.now(UTC).isoformat()
    conn = await db.connect()
    await conn.execute(
        """
        INSERT INTO listings (
            avito_id, url, title, price, status, first_seen, last_seen, watchlist_id
        )
        VALUES (
            '111', 'https://avito.ru/111', 'iPhone 17, 256 ГБ', 60000,
            'active', ?, ?, 'u1_iphone_17'
        )
        """,
        (now_iso, now_iso),
    )
    await conn.execute(
        """
        INSERT INTO events (id, avito_id, revision, kind, observed_at)
        VALUES ('evt_111', '111', 1, 'NEW_LISTING', ?)
        """,
        (now_iso,),
    )
    await conn.execute(
        """
        INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, created_at)
        VALUES ('del_111', 'evt_111', 1, 'telegram', 'sent', ?, ?)
        """,
        (now_iso, now_iso),
    )
    await conn.commit()

    report_dir = tmp_path / "reports"
    report_file = await generate_daily_report(db=db, reports_dir=report_dir)
    assert report_file.exists()
    content = report_file.read_text(encoding="utf-8")
    assert "# Daily Hunter Report" in content
    assert "iPhone 17" in content or "u1_iphone_17" in content
    assert "Total Notifications" in content or "buy_now" in content

    await db.close()
