from __future__ import annotations

from pathlib import Path

import aiosqlite

from hunter.core.db_parts.schema_path_01 import backup_db_before_migration


async def migrate_v3_to_v4(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v3")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_cache (
              photo_sha256 TEXT NOT NULL,
              sig_hash TEXT NOT NULL,
              method TEXT NOT NULL,
              provider TEXT NOT NULL,
              result_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (photo_sha256, sig_hash, method, provider)
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              call_type TEXT NOT NULL,
              product_id TEXT,
              avito_id TEXT,
              tokens_prompt INTEGER DEFAULT 0,
              tokens_completion INTEGER DEFAULT 0,
              tokens_total INTEGER DEFAULT 0,
              latency_ms INTEGER DEFAULT 0,
              status TEXT DEFAULT 'ok',
              error_msg TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts)")
        await conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_llm_calls_product ON '
            'llm_calls(product_id, ts)')

        await conn.execute("PRAGMA user_version = 4")
        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        raise


async def migrate_v4_to_v5(conn: aiosqlite.Connection, db_path: Path) -> None:
    await backup_db_before_migration(conn, db_path, "v4")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_shadow_results (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id TEXT NOT NULL,
              avito_id TEXT NOT NULL,
              ts TEXT NOT NULL,
              status TEXT NOT NULL,
              signal_t TEXT,
              signal_o TEXT,
              signal_d TEXT,
              signal_c TEXT,
              evidence TEXT,
              differences TEXT,
              warning_text TEXT,
              skip_reason TEXT,
              details TEXT,
              photo_paths TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_photo_shadow_prod_ts ON '
                'photo_shadow_results(product_id, ts)'
        )
        await conn.execute("PRAGMA user_version = 5")
        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        raise

