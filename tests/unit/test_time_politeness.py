from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import hunter.collect.politeness as politeness_mod
from hunter.collect.politeness import CircuitOpen, Politeness
from hunter.core.db import Database


@pytest.mark.asyncio
@pytest.mark.parametrize("tz_name", ["Europe/Moscow", "UTC"])
async def test_circuit_45_min_after_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tz_name: str
) -> None:
    monkeypatch.setenv("TZ", tz_name)
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    db_path = tmp_path / f"test_{tz_name.replace('/', '_')}.db"
    db = Database(db_path)
    await db.connect()
    polite = Politeness(db)

    clock = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
    if hasattr(politeness_mod, "now_utc"):
        monkeypatch.setattr(politeness_mod, "now_utc", lambda: clock)
    else:

        class FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    if tz_name == "Europe/Moscow":
                        return (clock + timedelta(hours=3)).replace(tzinfo=None)
                    return clock.replace(tzinfo=None)
                return clock.astimezone(tz)

            @classmethod
            def fromisoformat(cls, val):
                return datetime.fromisoformat(val)

        monkeypatch.setattr(politeness_mod, "datetime", FakeDatetime)

    # Trigger 429
    with pytest.raises(CircuitOpen):
        await polite.on_status(429)

    # Check stored circuit_until has timezone info (+00:00 or Z)
    stored_circuit = await db.get_runtime("circuit_until")
    assert stored_circuit is not None
    assert "+" in stored_circuit or "Z" in stored_circuit, (
        f"circuit_until '{stored_circuit}' lacks timezone"
    )

    # 44m 59s: still blocked
    clock = clock + timedelta(minutes=44, seconds=59)
    with pytest.raises(CircuitOpen):
        await polite.before_request()

    # Exactly 45m 00s: permitted
    clock = clock + timedelta(seconds=1)
    await polite.before_request()

    await db.close()


@pytest.mark.asyncio
async def test_legacy_circuit_reset_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    # Create database manually with user_version = 0 and legacy naive circuit_until
    import aiosqlite

    from hunter.core.db import SCHEMA_PATH

    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await conn.execute(
            "INSERT INTO runtime(key, value) VALUES ('circuit_until', '2026-10-04 12:00:00')"
        )
        await conn.execute("PRAGMA user_version = 0")
        await conn.commit()

    db = Database(db_path)
    await db.connect()
    # Migration should have cleared legacy naive circuit_until
    val = await db.get_runtime("circuit_until")
    assert val is None
    cur = await (await db.connect()).execute("PRAGMA user_version")
    ver = (await cur.fetchone())[0]
    assert ver >= 2
    await db.close()


def test_night_window_in_msk(monkeypatch: pytest.MonkeyPatch) -> None:
    # 02:30 MSK is 23:30 UTC of previous day
    # Should be night regardless of system TZ
    from hunter.core.config import get_settings

    base_settings = get_settings()
    base_settings.request_budget.night_enabled = True
    base_settings.request_budget.night_hours = [1, 7]
    monkeypatch.setattr(politeness_mod, "get_settings", lambda: base_settings)

    db = Database(Path(":memory:"))
    polite = Politeness(db)

    # 23:30 UTC is 02:30 MSK (Night)
    night_clock = datetime(2026, 10, 4, 23, 30, 0, tzinfo=UTC)
    if hasattr(politeness_mod, "now_utc"):
        monkeypatch.setattr(politeness_mod, "now_utc", lambda: night_clock)
    else:

        class FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return night_clock.replace(tzinfo=None)
                return night_clock.astimezone(tz)

        monkeypatch.setattr(politeness_mod, "datetime", FakeDatetime)
    assert polite.is_night() is True

    # 08:00 UTC is 11:00 MSK (Day)
    day_clock = datetime(2026, 10, 4, 8, 0, 0, tzinfo=UTC)
    if hasattr(politeness_mod, "now_utc"):
        monkeypatch.setattr(politeness_mod, "now_utc", lambda: day_clock)
    else:

        class FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return day_clock.replace(tzinfo=None)
                return day_clock.astimezone(tz)

        monkeypatch.setattr(politeness_mod, "datetime", FakeDatetime)
    assert polite.is_night() is False


def test_night_slowdown_disabled_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    from hunter.core.config import get_settings

    base_settings = get_settings()

    db = Database(Path(":memory:"))
    polite = Politeness(db)

    # 23:30 UTC is 02:30 MSK (Night by clock)
    night_clock = datetime(2026, 10, 4, 23, 30, 0, tzinfo=UTC)
    monkeypatch.setattr(politeness_mod, "now_utc", lambda: night_clock)

    # When night_enabled is True, it is night
    base_settings.request_budget.night_enabled = True
    base_settings.request_budget.night_hours = [1, 7]
    monkeypatch.setattr(politeness_mod, "get_settings", lambda: base_settings)
    assert polite.is_night() is True

    # When night_enabled is set to False, is_night() returns False
    base_settings.request_budget.night_enabled = False
    assert polite.is_night() is False

    # When night_hours is [0, 0], is_night() returns False
    base_settings.request_budget.night_enabled = True
    base_settings.request_budget.night_hours = [0, 0]
    assert polite.is_night() is False
