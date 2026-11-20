"""Regression test for F-043: Migration rollback tolerance.

Verifies that when code is rolled back to an older version (where latest_version < db_version),
MigrationRunner with allow_future_schema=True (or HUNTER_ALLOW_FUTURE_SCHEMA=1)
safely allows the service to boot without crashing with FutureSchemaVersionError.
"""

from __future__ import annotations

import os
from pathlib import Path
import aiosqlite
import pytest

from hunter.storage.migration_runner import (
    FutureSchemaVersionError,
    MigrationRunner,
)


@pytest.mark.asyncio
async def test_migration_runner_future_schema_rejected_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA user_version = 99")
    await conn.commit()

    runner = MigrationRunner(db_path)
    with pytest.raises(FutureSchemaVersionError):
        await runner.run_migrations(conn)

    await conn.close()


@pytest.mark.asyncio
async def test_migration_runner_future_schema_tolerated_with_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "rollback.db"
    conn = await aiosqlite.connect(db_path)

    # 1. Setup schema_migrations with v12..v20
    await conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, checksum TEXT, applied_at TEXT)"
    )
    from hunter.storage.migration_runner import MIGRATIONS
    for ver, name, chk, _ in MIGRATIONS:
        await conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, 'now')",
            (ver, name, chk),
        )
    # Simulate DB version 20 (future version)
    await conn.execute("PRAGMA user_version = 20")
    await conn.commit()

    # 2. Simulate rollback runner with older latest_version = 18 and allow_future_schema=True
    runner = MigrationRunner(db_path, allow_future_schema=True)
    runner.latest_version = 18

    # Must NOT raise FutureSchemaVersionError
    applied = await runner.run_migrations(conn)
    assert applied == 0

    await conn.close()


@pytest.mark.asyncio
async def test_migration_runner_future_schema_tolerated_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "env_rollback.db"
    conn = await aiosqlite.connect(db_path)

    await conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, checksum TEXT, applied_at TEXT)"
    )
    from hunter.storage.migration_runner import MIGRATIONS
    for ver, name, chk, _ in MIGRATIONS:
        await conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, 'now')",
            (ver, name, chk),
        )
    await conn.execute("PRAGMA user_version = 20")
    await conn.commit()

    monkeypatch.setenv("HUNTER_ALLOW_FUTURE_SCHEMA", "1")
    runner = MigrationRunner(db_path)
    runner.latest_version = 18

    applied = await runner.run_migrations(conn)
    assert applied == 0

    await conn.close()
