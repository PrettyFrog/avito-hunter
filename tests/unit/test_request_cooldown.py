"""Server retry deadlines are lower bounds, including values above one hour."""
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from hunter.domain.recovery_policy import compute_slot_backoff, parse_retry_after
from hunter.domain.slots import SlotSignal


@pytest.mark.asyncio
async def test_shared_ip_cooldown_can_be_extended_by_another_slot_generation(tmp_path):
    import sqlite3
    from hunter.storage.connection import StorageConfig, StorageConnectionManager
    from hunter.storage.request_cooldown import RequestCooldownStore
    from hunter.storage.writer import SQLiteWriterActor
    path = tmp_path / "shared.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runtime(key TEXT PRIMARY KEY, value TEXT)")
    manager = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(manager)
    await writer.start()
    try:
        store = RequestCooldownStore(writer, wall_clock=lambda: 1000)
        await store.extend("same-ip", "avito", generation=99, duration_sec=100)
        later = await store.extend("same-ip", "avito", generation=1,
                                   duration_sec=9000, shared_egress=True)
        assert later.generation == 99 and later.until_utc == 10000
    finally:
        await writer.stop()
        await manager.drain_and_close()


@pytest.mark.parametrize("raw", ["9000", 9000, 9000.0])
def test_retry_after_seconds_is_not_capped(raw):
    assert parse_retry_after(raw) == 9000
    assert compute_slot_backoff(SlotSignal.HTTP_429, 0, ra=9000) >= 9000
    assert compute_slot_backoff(SlotSignal.TIMEOUT, 0, req=9000) >= 9000


def test_retry_after_date_is_not_capped_and_invalid_is_not_guessed():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=9000)), now) == 9000
    for raw in ("invalid", "-1", "nan", "inf", ""):
        assert parse_retry_after(raw, now) is None


def test_huge_numeric_retry_after_is_a_hold_not_an_invalid_header():
    now = datetime(2026, 11, 5, tzinfo=UTC)
    huge = "9" * 400
    value = parse_retry_after(huge, now)
    assert value is not None and value > 10**9
    from hunter.domain.recovery_policy import saturated_deadline
    assert saturated_deadline(now, value) == datetime.max.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_writer_restart_stale_updates_and_clock_rollback(tmp_path):
    import asyncio
    import sqlite3
    from hunter.storage.connection import StorageConfig, StorageConnectionManager
    from hunter.storage.request_cooldown import RequestCooldownStore
    from hunter.storage.writer import SQLiteWriterActor
    path = tmp_path / "cooldown.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runtime(key TEXT PRIMARY KEY, value TEXT)")
    manager = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(manager)
    await writer.start()
    try:
        store = RequestCooldownStore(writer, wall_clock=lambda: 1000)
        records = await asyncio.gather(*[
            store.extend("shared-ip", "avito", generation=2, duration_sec=seconds)
            for seconds in (9000, 100, 200)
        ])
        assert all(record.until_utc == 10000 for record in records)
        stale = await store.extend("shared-ip", "avito", generation=1, duration_sec=20000)
        assert stale.generation == 2 and stale.until_utc == 10000
        assert await store.load("different-ip", "avito") is None
        assert await store.load("shared-ip", "captcha") is None
    finally:
        await writer.stop()
        await manager.drain_and_close()
    manager = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(manager)
    await writer.start()
    try:
        restored = await RequestCooldownStore(writer).load("shared-ip", "avito")
        assert restored.monotonic_deadline(utc_now=2000, mono_now=50) == 8050
        assert restored.monotonic_deadline(utc_now=900, mono_now=50) >= 9050
        assert restored.monotonic_deadline(utc_now=11000, mono_now=50) == 50
    finally:
        await writer.stop()
        await manager.drain_and_close()


def test_cooldown_survives_abrupt_process_exit(tmp_path):
    import os
    from pathlib import Path
    import sqlite3
    import subprocess
    import sys
    script = '''
import asyncio, os, sys
from pathlib import Path
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor
from hunter.storage.request_cooldown import RequestCooldownStore
async def main():
    manager = StorageConnectionManager(StorageConfig(db_path=Path(sys.argv[1])))
    writer = SQLiteWriterActor(manager)
    await writer.start()
    await RequestCooldownStore(writer, wall_clock=lambda:1000).extend(
        "ip", "avito", generation=2, duration_sec=9000)
    os._exit(42)
asyncio.run(main())
'''
    path = tmp_path / "crash.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runtime(key TEXT PRIMARY KEY, value TEXT)")
    result = subprocess.run([sys.executable, "-c", script, str(path)], timeout=20,
                            env={**os.environ, "PYTHONPATH": str(Path("src").resolve())})
    assert result.returncode == 42
    with sqlite3.connect(path) as conn:
        value = conn.execute("SELECT json_extract(value, '$.until_utc') FROM runtime").fetchone()
        assert value == (10000,)
