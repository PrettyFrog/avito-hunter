"""Versioned migration runner for schema v12+ with checksums, rollback and backup (S3 / T-461)."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from hunter.storage.migrations.v012_subscriptions import V012_SQL, apply_v012_migration
from hunter.storage.migrations.v013_events import V013_SQL, apply_v013_migration
from hunter.storage.migrations.v014_slots import V014_SQL, apply_v014_migration
from hunter.storage.migrations.v015_job_tokens import V015_SQL, apply_v015_migration
from hunter.storage.migrations.v016_query_groups import V016_SQL, apply_v016_migration
from hunter.storage.migrations.v017_binding_nonces import V017_SQL, apply_v017_migration
from hunter.storage.migrations.v018_slot_roles import (
    V018_CHECKSUM_INPUT,
    apply_v018_migration,
)
from hunter.storage.migrations.v019_products_schema import (
    V019_CHECKSUM_INPUT,
    apply_v019_migration,
)

from hunter.storage.migrations.v020_subscription_names import (
    V020_CHECKSUM_INPUT,
    apply_v020_migration,
)
from hunter.storage.migrations.v021_subscription_keywords import (
    V021_CHECKSUM_INPUT,
    apply_v021_migration,
)
from hunter.storage.migrations.v022_drop_legacy_tables import (
    V022_CHECKSUM_INPUT,
    apply_v022_migration,
)

logger = logging.getLogger(__name__)


class MigrationError(Exception):
    """Base error for migration failures."""


class FutureSchemaVersionError(MigrationError):
    """Raised when database schema version is newer than supported."""


class MigrationChecksumError(MigrationError):
    """Raised when an applied migration has a conflicting checksum."""


SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _calc_checksum(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode("utf-8")).hexdigest()[:16]


MIGRATIONS: list[tuple[int, str, str, Any]] = [
    (12, "v012_subscriptions", _calc_checksum(V012_SQL), apply_v012_migration),
    (13, "v013_events", _calc_checksum(V013_SQL), apply_v013_migration),
    (14, "v014_slots", _calc_checksum(V014_SQL), apply_v014_migration),
    (15, "v015_job_tokens", _calc_checksum(V015_SQL), apply_v015_migration),
    (16, "v016_query_groups", _calc_checksum(V016_SQL), apply_v016_migration),
    (17, "v017_binding_nonces", _calc_checksum(V017_SQL), apply_v017_migration),
    (18, "v018_slot_roles", _calc_checksum(V018_CHECKSUM_INPUT), apply_v018_migration),
    (19, "v019_products_schema", _calc_checksum(V019_CHECKSUM_INPUT), apply_v019_migration),
    (20, "v020_subscription_names", _calc_checksum(V020_CHECKSUM_INPUT), apply_v020_migration),
    (21, "v021_subscription_keywords", _calc_checksum(V021_CHECKSUM_INPUT), apply_v021_migration),
    (22, "v022_drop_legacy_tables", _calc_checksum(V022_CHECKSUM_INPUT), apply_v022_migration),
]

LATEST_VERSION = 22


class MigrationRunner:
    """Manages forward schema migrations for v12+."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        allow_future_schema: bool | None = None,
    ) -> None:
        self.db_path = db_path
        self.latest_version = LATEST_VERSION
        if allow_future_schema is not None:
            self.allow_future_schema = allow_future_schema
        else:
            self.allow_future_schema = os.getenv(
                "HUNTER_ALLOW_FUTURE_SCHEMA", "0"
            ).lower() in ("1", "true")

    def _backup(self, current_ver: int) -> None:
        if not self.db_path or not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return
        backup_path = self.db_path.with_name(f"{self.db_path.name}.bak.v{current_ver}")
        if backup_path.exists():
            backup_path = self.db_path.with_name(
                f"{self.db_path.name}.bak.v{current_ver}.{time.time_ns()}"
            )
        src = sqlite3.connect(self.db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    async def _run_sql(self, conn: Any, sql: str) -> None:
        call = conn.execute(sql)
        if inspect.isawaitable(call):
            await call

    async def _verify_applied_checksums(self, conn: Any) -> dict[int, str]:
        await self._run_sql(conn, SCHEMA_MIGRATIONS_TABLE_SQL)
        check_call = conn.execute("SELECT version, checksum FROM schema_migrations")
        check_cur = await check_call if inspect.isawaitable(check_call) else check_call
        raw_rows = check_cur.fetchall()
        applied_rows = await raw_rows if inspect.isawaitable(raw_rows) else raw_rows
        applied_map = {r[0]: r[1] for r in applied_rows}

        for ver, name, chk, _ in MIGRATIONS:
            if ver in applied_map and applied_map[ver] != chk:
                raise MigrationChecksumError(
                    f"Checksum mismatch for migration v{ver} ({name}): "
                    f"recorded {applied_map[ver]} != current {chk}"
                )
        return applied_map

    async def run_migrations(self, conn: Any) -> int:
        """Run all pending v12+ migrations with checksum and future guards."""
        cur_call = conn.execute("PRAGMA user_version")
        cur = await cur_call if inspect.isawaitable(cur_call) else cur_call
        row_call = cur.fetchone()
        row = await row_call if inspect.isawaitable(row_call) else row_call
        version = int(row[0]) if row else 0

        if version > self.latest_version:
            if self.allow_future_schema:
                logger.warning(
                    "Database schema version %d exceeds code latest_version %d (rollback tolerance active).",
                    version,
                    self.latest_version,
                )
                await self._verify_applied_checksums(conn)
                return 0
            raise FutureSchemaVersionError(
                f"Database version {version} exceeds maximum supported {self.latest_version}"
            )

        applied_map = await self._verify_applied_checksums(conn)

        # Apply pending migrations
        for ver, name, chk, func in MIGRATIONS:
            if ver == version and ver not in applied_map:
                # A migration may commit its schema and user_version before this
                # runner records the checksum. Reconcile that crash window on retry.
                await self._run_sql(
                    conn,
                    f"INSERT INTO schema_migrations (version, name, checksum) "
                    f"VALUES ({ver}, '{name}', '{chk}')",
                )
                commit_call = conn.commit()
                if inspect.isawaitable(commit_call):
                    await commit_call
                applied_map[ver] = chk
            if version < ver:
                self._backup(version)
                await func(conn, self.db_path)
                await self._run_sql(
                    conn,
                    f"INSERT OR REPLACE INTO schema_migrations (version, name, checksum) VALUES ({ver}, '{name}', '{chk}')",
                )
                commit_call = conn.commit()
                if inspect.isawaitable(commit_call):
                    await commit_call
                version = ver

        return version
