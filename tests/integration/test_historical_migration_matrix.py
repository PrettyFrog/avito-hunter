"""Upgrade real schema snapshots captured from the corresponding historical commits."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from hunter.core.db import Database
from hunter.storage import migration_runner
from hunter.storage.migrations import v018_slot_roles, v019_products_schema

FIXTURES = Path(__file__).parents[1] / "fixtures" / "migrations"
HISTORICAL_VERSIONS = (1, 6, 11, 12, 14, 16)


def _make_historical_db(path: Path, version: int) -> None:
    sql = gzip.decompress((FIXTURES / f"v{version}.sql.gz").read_bytes()).decode()
    with sqlite3.connect(path) as conn:
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute(
            "INSERT INTO listings (avito_id, url, first_seen, last_seen, status, title) "
            "VALUES ('matrix-listing', 'https://avito.ru/item', 't0', 't0', 'active', 'kept')"
        )
        if version == 1:
            conn.execute(
                "INSERT INTO products (model_id, category) VALUES ('matrix-product', 'electronics')"
            )
        else:
            conn.execute(
                "INSERT INTO products (id, model_id, category) "
                "VALUES ('matrix-product', 'matrix-product', 'electronics')"
            )


def _products_signature(path: Path) -> dict[str, tuple[str, int, str]]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("PRAGMA table_info(products)").fetchall()
    return {row[1]: (row[2].upper(), row[3], str(row[4])) for row in rows}


async def _upgrade_and_assert(path: Path) -> None:
    db = Database(path)
    conn = await db.connect()
    version = (await (await conn.execute("PRAGMA user_version")).fetchone())[0]
    title = (
        await (
            await conn.execute("SELECT title FROM listings WHERE avito_id = 'matrix-listing'")
        ).fetchone()
    )[0]
    tables = {
        row[0]
        for row in await (
            await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    assert version == migration_runner.LATEST_VERSION
    columns = await (await conn.execute("PRAGMA table_info(subscriptions)")).fetchall()
    col_names = {row[1] for row in columns}
    assert "name" in col_names
    assert "manual_keywords" in col_names
    assert title == "kept"
    assert {"subscriptions", "events", "deliveries", "slots", "price_watch_jobs"} <= tables
    assert "products" not in tables
    await db.close()
    fresh_path = path.with_name("fresh-reference.db")
    fresh = Database(fresh_path)
    await fresh.connect()
    await fresh.close()
    assert _products_signature(path) == _products_signature(fresh_path)
    async with aiosqlite.connect(path) as check:
        assert (await (await check.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"
        assert await (await check.execute("PRAGMA foreign_key_check")).fetchall() == []
    if int(path.stem.rsplit("v", 1)[-1]) >= 12:
        backup = path.with_name(f"{path.name}.bak.v{path.stem.rsplit('v', 1)[-1]}")
        assert backup.exists()
        with sqlite3.connect(backup) as check:
            assert check.execute("PRAGMA user_version").fetchone()[0] == int(
                path.stem.rsplit("v", 1)[-1]
            )
            assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", HISTORICAL_VERSIONS)
async def test_historical_schema_upgrades_and_preserves_listing(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / f"historical-v{version}.db"
    _make_historical_db(path, version)
    await _upgrade_and_assert(path)


@pytest.mark.asyncio
async def test_fault_v18_failure_rolls_back_and_retry_completes(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "crash-v18.db"
    _make_historical_db(path, 16)
    conn = await aiosqlite.connect(path)
    original_migrations = migration_runner.MIGRATIONS
    through_v17 = [migration for migration in original_migrations if migration[0] <= 17]
    monkeypatch.setattr(migration_runner, "MIGRATIONS", through_v17)
    runner = migration_runner.MigrationRunner(path)
    assert await runner.run_migrations(conn) == 17

    only_v18 = [migration for migration in original_migrations if migration[0] == 18]
    monkeypatch.setattr(migration_runner, "MIGRATIONS", only_v18)
    original_run = v018_slot_roles._run

    async def fail_after_alter(connection, sql: str) -> None:
        if sql == v018_slot_roles.V018_BACKFILL_SQL:
            raise RuntimeError("injected interruption after ALTER TABLE")
        await original_run(connection, sql)

    monkeypatch.setattr(v018_slot_roles, "_run", fail_after_alter)
    with pytest.raises(RuntimeError, match="injected interruption"):
        await runner.run_migrations(conn)
    columns = await (await conn.execute("PRAGMA table_info(slots)")).fetchall()
    version = (await (await conn.execute("PRAGMA user_version")).fetchone())[0]
    assert "role" not in {row[1] for row in columns}
    assert version == 17

    monkeypatch.setattr(v018_slot_roles, "_run", original_run)
    assert await runner.run_migrations(conn) == 18
    columns = await (await conn.execute("PRAGMA table_info(slots)")).fetchall()
    assert "role" in {row[1] for row in columns}
    await conn.close()


@pytest.mark.asyncio
async def test_fault_v19_failure_preserves_legacy_products_and_retry_normalizes(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "products-v19.db"
    _make_historical_db(path, 1)
    previous = migration_runner.MIGRATIONS
    through_v18 = [migration for migration in previous if migration[0] <= 18]
    monkeypatch.setattr(migration_runner, "MIGRATIONS", through_v18)
    legacy_db = Database(path)
    await legacy_db.connect()
    await legacy_db.close()
    conn = await aiosqlite.connect(path)
    runner = migration_runner.MigrationRunner(path)
    v19 = [item for item in previous if item[0] == 19]
    monkeypatch.setattr(migration_runner, "MIGRATIONS", v19)
    original_call = v019_products_schema._call

    async def fail_before_rename(connection, sql: str, params: tuple = ()) -> object:
        if sql == "ALTER TABLE products_v19 RENAME TO products":
            raise RuntimeError("injected v19 interruption")
        return await original_call(connection, sql, params)

    monkeypatch.setattr(v019_products_schema, "_call", fail_before_rename)
    with pytest.raises(RuntimeError, match="injected v19 interruption"):
        await runner.run_migrations(conn)
    version = await (await conn.execute("PRAGMA user_version")).fetchone()
    columns = await (await conn.execute("PRAGMA table_info(products)")).fetchall()
    assert version[0] == 18
    assert next(row for row in columns if row[1] == "model_id")[5] == 1
    assert await (
        await conn.execute("SELECT category FROM products WHERE model_id = 'matrix-product'")
    ).fetchone() == ("electronics",)
    monkeypatch.setattr(v019_products_schema, "_call", original_call)
    assert await runner.run_migrations(conn) == 19
    final_columns = await (await conn.execute("PRAGMA table_info(products)")).fetchall()
    assert next(row for row in final_columns if row[1] == "id")[5] == 1
    await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_version", range(12, 21))
async def test_fault_migration_commit_before_checksum_is_reconciled(
    tmp_path: Path, monkeypatch, target_version: int
) -> None:
    path = tmp_path / "migration-history-gap.db"
    _make_historical_db(path, 11)
    conn = await aiosqlite.connect(path)
    migrations = migration_runner.MIGRATIONS
    prior = [item for item in migrations if item[0] < target_version]
    if prior:
        monkeypatch.setattr(migration_runner, "MIGRATIONS", prior)
        await migration_runner.MigrationRunner(path).run_migrations(conn)
    migration = next(item for item in migrations if item[0] == target_version)
    monkeypatch.setattr(migration_runner, "MIGRATIONS", [migration])
    runner = migration_runner.MigrationRunner(path)
    original_run_sql = runner._run_sql

    async def fail_history_insert(connection, sql: str) -> None:
        if sql.startswith("INSERT OR REPLACE INTO schema_migrations"):
            raise RuntimeError("injected crash after migration commit")
        await original_run_sql(connection, sql)

    monkeypatch.setattr(runner, "_run_sql", fail_history_insert)
    with pytest.raises(RuntimeError, match="after migration commit"):
        await runner.run_migrations(conn)
    assert (await (await conn.execute("PRAGMA user_version")).fetchone())[0] == target_version

    monkeypatch.setattr(runner, "_run_sql", original_run_sql)
    assert await runner.run_migrations(conn) == target_version
    history = await (
        await conn.execute(
            "SELECT version, checksum FROM schema_migrations WHERE version = ?",
            (target_version,),
        )
    ).fetchone()
    assert history == (target_version, migration[2])
    await conn.close()


@pytest.mark.asyncio
async def test_future_schema_version_is_rejected_without_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "future-schema.db"
    future_version = migration_runner.LATEST_VERSION + 1
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {future_version}")
    conn = await aiosqlite.connect(path)
    with pytest.raises(migration_runner.FutureSchemaVersionError):
        await migration_runner.MigrationRunner(path).run_migrations(conn)
    version = await (await conn.execute("PRAGMA user_version")).fetchone()
    assert version[0] == future_version
    await conn.close()
