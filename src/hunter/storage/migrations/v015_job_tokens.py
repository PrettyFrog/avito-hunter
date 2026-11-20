"""Migration v015: Additive lease_token column and index on jobs table (S3 / T-467)."""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

V015_SQL = """
ALTER TABLE jobs ADD COLUMN lease_token TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_lease_token ON jobs(lease_token) WHERE lease_token IS NOT NULL;
"""


def _create_backup(db_path: Path) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0 or (bak := db_path.with_name(f"{db_path.name}.bak.v14")).exists():
        return
    src, dst = sqlite3.connect(db_path), sqlite3.connect(bak)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def _run(conn: Any, sql: str) -> Any:
    call = conn.execute(sql)
    return await call if inspect.isawaitable(call) else call


async def apply_v015_migration(conn: Any, path: Path | None = None) -> None:
    """Apply migration v15: additive lease_token on jobs table."""
    if path:
        _create_backup(path)
    await _run(conn, "BEGIN IMMEDIATE")
    try:
        cur = await _run(conn, "PRAGMA table_info(jobs)")
        rows = cur.fetchall()
        cols = [r[1] for r in (await rows if inspect.isawaitable(rows) else rows)]
        if "lease_token" not in cols:
            await _run(conn, "ALTER TABLE jobs ADD COLUMN lease_token TEXT")
        await _run(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_jobs_lease_token ON jobs(lease_token) WHERE lease_token IS NOT NULL",
        )
        await _run(conn, "PRAGMA user_version = 15")
        commit_call = conn.commit()
        if inspect.isawaitable(commit_call):
            await commit_call
    except Exception:
        rollback_call = conn.rollback()
        if inspect.isawaitable(rollback_call):
            await rollback_call
        raise



