from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from hunter.core.config import get_settings
from hunter.core.models import now_utc

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def init_db(db: Any, path: Path | str | None = None) -> None:
    settings = get_settings()
    db.path = Path(path) if path else settings.db_path
    db._conn = None
    db._write_lock = None
    db._last_lease_recovery_ts = 0.0


def get_write_lock(db: Any) -> asyncio.Lock:
    if db._write_lock is None:
        db._write_lock = asyncio.Lock()
    return db._write_lock


def _reset_stale_connection(db: Any) -> None:
    if db._conn is not None:
        try:
            if not getattr(db._conn, "_running", True):
                db._conn = None
        except Exception:
            db._conn = None


async def _prepare_connection(db: Any, is_new: bool) -> None:
    db._conn.row_factory = aiosqlite.Row
    await db._conn.execute("PRAGMA journal_mode=WAL")
    await db._conn.execute("PRAGMA busy_timeout=15000")
    await db._conn.execute("PRAGMA foreign_keys=ON")
    await db._conn.execute("PRAGMA wal_autocheckpoint=1000")
    if is_new:
        await db._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await db._conn.execute("PRAGMA user_version = 11")
        await db.migrate()
    else:
        await db.migrate()
        await db._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    await db._conn.commit()
    tbls = [
        r[0]
        for r in await (
            await db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    ]
    if "products" in tbls:
        await db._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_id ON products(id)"
        )
        await db._conn.commit()


async def connect_db(db: Any) -> aiosqlite.Connection:
    _reset_stale_connection(db)
    if db._conn is None:
        db.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not db.path.exists() or db.path.stat().st_size == 0
        db._conn = await aiosqlite.connect(db.path)
        try:
            await _prepare_connection(db, is_new)
        except BaseException:
            try:
                await db._conn.close()
            finally:
                db._conn = None
            raise
    return db._conn


async def backup_db_before_migration(
    conn: aiosqlite.Connection,
    db_path: Path,
    version_tag: str,
) -> None:
    if db_path.exists() and db_path.stat().st_size > 0:
        backup_path = db_path.with_name(f"{db_path.name}.bak.{version_tag}")
        if backup_path.exists():
            ts_str = now_utc().strftime("%Y%m%d_%H%M%S")
            backup_path = db_path.with_name(f"{db_path.name}.bak.{version_tag}.{ts_str}")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await conn.execute(f"VACUUM INTO '{backup_path}'")
            logger.info("Database migration backup created at {}", backup_path)
        except Exception as exc:
            logger.error("Database migration backup failed at {}: {}", backup_path, exc)
            raise RuntimeError(
                f"Database migration aborted: backup failed ({backup_path}): {exc}"
            ) from exc


async def migrate_v0_to_v1(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v0")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        listing_cols = [
            r[1] for r in await (await conn.execute("PRAGMA table_info(listings)")).fetchall()
        ]
        if "price_suspect" not in listing_cols:
            await conn.execute("ALTER TABLE listings ADD COLUMN price_suspect INTEGER DEFAULT 0")

        metric_cols = [
            r[1] for r in await (await conn.execute("PRAGMA table_info(metrics)")).fetchall()
        ]
        if "hits_total" not in metric_cols:
            await conn.execute("ALTER TABLE metrics ADD COLUMN hits_total INTEGER DEFAULT 0")
        if "hits_no_price" not in metric_cols:
            await conn.execute("ALTER TABLE metrics ADD COLUMN hits_no_price INTEGER DEFAULT 0")
        if "anchor_fallback" not in metric_cols:
            await conn.execute("ALTER TABLE metrics ADD COLUMN anchor_fallback INTEGER DEFAULT 0")

        await conn.execute(
            "DELETE FROM runtime WHERE key = 'circuit_until' AND value NOT LIKE '%+%' "
                "AND value NOT LIKE '%Z%'"
        )
        await conn.execute("PRAGMA user_version = 1")
        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        raise


async def migrate_v1_to_v2(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v1")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        notif_cols = [
            r[1] for r in await (await conn.execute("PRAGMA table_info(notifications)")).fetchall()
        ]
        if notif_cols:
            if "status" not in notif_cols:
                await conn.execute("ALTER TABLE notifications ADD COLUMN status TEXT DEFAULT 'sent'")
            if "attempts" not in notif_cols:
                await conn.execute("ALTER TABLE notifications ADD COLUMN attempts INTEGER DEFAULT 1")
            if "next_try_at" not in notif_cols:
                await conn.execute("ALTER TABLE notifications ADD COLUMN next_try_at TEXT")
            if "payload" not in notif_cols:
                await conn.execute("ALTER TABLE notifications ADD COLUMN payload TEXT")
            if "created_at" not in notif_cols:
                await conn.execute("ALTER TABLE notifications ADD COLUMN created_at TEXT")

        metric_cols = [
            r[1] for r in await (await conn.execute("PRAGMA table_info(metrics)")).fetchall()
        ]
        if metric_cols:
            if "empty_ok" not in metric_cols:
                await conn.execute("ALTER TABLE metrics ADD COLUMN empty_ok INTEGER DEFAULT 0")
            if "unknown_empty" not in metric_cols:
                await conn.execute("ALTER TABLE metrics ADD COLUMN unknown_empty INTEGER DEFAULT 0")
            if "carousel_discarded" not in metric_cols:
                await conn.execute("ALTER TABLE metrics ADD COLUMN carousel_discarded INTEGER DEFAULT 0")
            if "shadow_cards_rejected" not in metric_cols:
                await conn.execute("ALTER TABLE metrics ADD COLUMN shadow_cards_rejected INTEGER DEFAULT 0")

        await conn.execute("PRAGMA user_version = 2")
        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        raise


async def _create_v3_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          product_id TEXT NOT NULL,
          dedupe_key TEXT NOT NULL,
          payload TEXT NOT NULL,
          priority INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 3,
          next_run_at TEXT,
          lease_until TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe 
        ON jobs(kind, dedupe_key) 
        WHERE status IN ('queued', 'leased')
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_fetch 
        ON jobs(status, priority DESC, id ASC)
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS request_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          product_id TEXT,
          req_type TEXT NOT NULL,
          status INTEGER,
          url TEXT
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts)")
    await conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_request_log_product ON '
        'request_log(product_id, ts)')


async def _migrate_v3_products(conn: aiosqlite.Connection) -> None:
    prod_cols = [
        r[1] for r in await (await conn.execute("PRAGMA table_info(products)")).fetchall()
    ]
    if not prod_cols:
        return
    if "id" not in prod_cols:
        await conn.execute("ALTER TABLE products ADD COLUMN id TEXT")
    if "version" not in prod_cols:
        await conn.execute("ALTER TABLE products ADD COLUMN version INTEGER DEFAULT 1")
    if "state" not in prod_cols:
        await conn.execute("ALTER TABLE products ADD COLUMN state TEXT DEFAULT 'active'")
    if "created_at" not in prod_cols:
        await conn.execute("ALTER TABLE products ADD COLUMN created_at TEXT DEFAULT ''")
    if "updated_at" not in prod_cols:
        await conn.execute("ALTER TABLE products ADD COLUMN updated_at TEXT DEFAULT ''")

    await conn.execute(
        "UPDATE products SET id = model_id, created_at = datetime('now') WHERE id "
            "IS NULL OR id = ''"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_id ON products(id)"
    )


async def migrate_v2_to_v3(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v2")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await _create_v3_tables(conn)
        await _migrate_v3_products(conn)
        await conn.execute("PRAGMA user_version = 3")
        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        raise


