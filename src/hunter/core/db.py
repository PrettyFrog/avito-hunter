from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
import aiosqlite
from loguru import logger
from hunter.core.db_parts.schema_path_01 import SCHEMA_PATH, connect_db, get_write_lock, init_db, migrate_v0_to_v1, migrate_v1_to_v2, migrate_v2_to_v3
from hunter.core.db_parts.database_migrate_04 import migrate_v3_to_v4, migrate_v4_to_v5
from hunter.core.db_parts.database_checkpoint_13 import checkpoint_db, close_db
from hunter.core.db_parts.database_migrate_06 import create_v7_user_tables, migrate_v5_to_v6
from hunter.core.db_parts.database_migrate_08 import apply_v7_user_migration, migrate_v7_to_v8
from hunter.core.db_parts.database_migrate_10 import migrate_v8_to_v9, migrate_v9_to_v10, migrate_v10_to_v11
from hunter.core.db_parts.database_get_recent_shadow_overlaps_54 import get_db_singleton, lifespan_db_ctx
from hunter.core.models import now_utc, utcnow
from hunter.core.db_parts.database_api_01 import _DatabaseApi01
from hunter.core.db_parts.database_api_02 import _DatabaseApi02
from hunter.core.db_parts.database_api_03 import _DatabaseApi03
from hunter.core.db_parts.database_api_04 import _DatabaseApi04
from hunter.core.db_parts.database_api_05 import _DatabaseApi05
from hunter.core.db_parts.database_api_06 import _DatabaseApi06


__all__ = [
    "SCHEMA_PATH",
    "Database",
    "get_db",
    "lifespan_db",
]

async def _migrate_v6_to_v7(conn: aiosqlite.Connection, db_path: Path) -> None:
    if db_path.exists() and db_path.stat().st_size > 0:
        backup_path = db_path.with_name(f"{db_path.name}.bak.v6")
        counter = 1
        while backup_path.exists():
            ts_str = now_utc().strftime("%Y%m%d_%H%M%S_%f")
            backup_path = db_path.with_name(f"{db_path.name}.bak.v6.{ts_str}_{counter}")
            counter += 1
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await conn.execute(f"VACUUM INTO '{backup_path}'")
            logger.info("Database migration backup created at {}", backup_path)
        except Exception as exc:
            logger.error("Database migration backup failed at {}: {}", backup_path, exc)
            raise RuntimeError(
                f"Database migration aborted: backup failed ({backup_path}): {exc}"
            ) from exc
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await create_v7_user_tables(conn)
        await apply_v7_user_migration(conn)
        await conn.execute("PRAGMA user_version = 7")
        await conn.commit()
        logger.info("Database migration to version 7 (Multi-User M1) completed successfully")
    except Exception:
        await conn.execute("ROLLBACK")
        raise

async def _ensure_extra_columns(conn: aiosqlite.Connection) -> None:
    metric_cols = [
        r[1] for r in await (await conn.execute("PRAGMA table_info(metrics)")).fetchall()
    ]
    if "shadow_cards_rejected" not in metric_cols:
        await conn.execute("ALTER TABLE metrics ADD COLUMN shadow_cards_rejected INTEGER DEFAULT 0")
        await conn.commit()
    if "light_ok" not in metric_cols:
        await conn.execute("ALTER TABLE metrics ADD COLUMN light_ok INTEGER DEFAULT 0")
        await conn.commit()
    if "light_fallbacks" not in metric_cols:
        await conn.execute("ALTER TABLE metrics ADD COLUMN light_fallbacks INTEGER DEFAULT 0")
        await conn.commit()

    req_cols = [
        r[1] for r in await (await conn.execute("PRAGMA table_info(request_log)")).fetchall()
    ]
    if "latency_ms" not in req_cols:
        await conn.execute("ALTER TABLE request_log ADD COLUMN latency_ms INTEGER DEFAULT 0")
    if "error" not in req_cols:
        await conn.execute("ALTER TABLE request_log ADD COLUMN error TEXT")

    inv_cols = [
        r[1] for r in await (await conn.execute("PRAGMA table_info(invites)")).fetchall()
    ]
    if "used_at" not in inv_cols:
        await conn.execute("ALTER TABLE invites ADD COLUMN used_at TEXT")

    notif_cols = [
        r[1] for r in await (await conn.execute("PRAGMA table_info(notifications)")).fetchall()
    ]
    if notif_cols and "photo_key" not in notif_cols:
        await conn.execute("ALTER TABLE notifications ADD COLUMN photo_key TEXT")

async def _ensure_extra_tables_and_indexes(conn: aiosqlite.Connection) -> None:
    tables = [r[0] for r in await (await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]
    if "notifications" in tables:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_notif_photo_key ON notifications(photo_key)")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS health_daily (
          date TEXT PRIMARY KEY,
          total_reqs INTEGER DEFAULT 0,
          err_429 INTEGER DEFAULT 0,
          err_403 INTEGER DEFAULT 0,
          captcha_count INTEGER DEFAULT 0,
          timeout_count INTEGER DEFAULT 0,
          timeout_ratio REAL DEFAULT 0.0,
          latency_median_ms REAL DEFAULT 0.0,
          latency_p90_ms REAL DEFAULT 0.0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    if "evaluations" in tables:
        await conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_evaluations_avito_id ON '
            'evaluations(avito_id)')
        await conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_evaluations_created_at ON '
            'evaluations(created_at)')
    await conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_listing_events_avito_id ON '
        'listing_events(avito_id)')
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_listing_events_ts ON listing_events(ts)")
    await conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_listings_watchlist_id ON '
        'listings(watchlist_id)')
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_images_avito_id ON images(avito_id)")

_db: Database | None = None

@asynccontextmanager
async def lifespan_db() -> AsyncIterator[Database]:
    async with lifespan_db_ctx(Database) as db:
        yield db


class Database(_DatabaseApi01, _DatabaseApi02, _DatabaseApi03, _DatabaseApi04, _DatabaseApi05, _DatabaseApi06):
        def __init__(self, path: Path | str | None = None) -> None:
            init_db(self, path=path)

        @property
        def write_lock(self) -> asyncio.Lock:
            return get_write_lock(self)

        async def connect(self) -> aiosqlite.Connection:
            return await connect_db(self)

        async def migrate(self) -> None:
            assert self._conn is not None
            cur = await self._conn.execute("PRAGMA user_version")
            row = await cur.fetchone()
            version = int(row[0]) if row else 0
            if version < 1:
                await migrate_v0_to_v1(self._conn, self.path)
            if version < 2:
                await migrate_v1_to_v2(self._conn, self.path)
            if version < 3:
                await migrate_v2_to_v3(self._conn, self.path)
            if version < 4:
                await migrate_v3_to_v4(self._conn, self.path)
            if version < 5:
                await migrate_v4_to_v5(self._conn, self.path)
            if version < 6:
                await migrate_v5_to_v6(self._conn, self.path)
            if version < 7:
                await _migrate_v6_to_v7(self._conn, self.path)
            if version < 8:
                await migrate_v7_to_v8(self._conn, self.path)
            if version < 9:
                await migrate_v8_to_v9(self._conn, self.path)
            if version < 10:
                await migrate_v9_to_v10(self._conn, self.path)
            if version < 11:
                await migrate_v10_to_v11(self._conn, self.path)

            from hunter.storage.migration_runner import MigrationRunner
            await MigrationRunner(self.path).run_migrations(self._conn)

            await _ensure_extra_columns(self._conn)
            await _ensure_extra_tables_and_indexes(self._conn)
            await self._conn.commit()

        async def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int] | None:
            """Flushes SQLite WAL to main database. Modes: PASSIVE, FULL, RESTART, TRUNCATE."""
            return await checkpoint_db(self, mode=mode)

        async def close(self) -> None:
            await close_db(self)


def get_db() -> Database:
    global _db
    if _db is not None:
        return _db
    return get_db_singleton(Database)
