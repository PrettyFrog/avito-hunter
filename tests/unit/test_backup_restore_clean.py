from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from hunter.core.db import Database
from ops.backup import create_backup, restore_backup, verify_backup


@pytest.mark.asyncio
async def test_clean_database_initialization_and_schema_version(tmp_path: Path) -> None:
    """A fresh database initializes with all tables, zero FK violations, and schema >= 19."""
    db_path = tmp_path / "clean_hunter.db"
    db = Database(db_path)
    await db.connect()
    try:
        await db.migrate()
    finally:
        await db.close()

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        version = cur.execute("PRAGMA user_version").fetchone()[0]
        assert version >= 19, f"Expected schema version >= 19, got {version}"

        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"

        fk_violations = cur.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"Unexpected foreign key violations: {fk_violations}"

        tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        required_tables = {
            "users", "queries", "subscriptions", "query_membership",
            "subscription_seen", "listing_state", "events", "recipient_events",
            "deliveries", "slots", "assignments", "session_versions", "price_watch_jobs"
        }
        missing = required_tables - tables
        assert not missing, f"Missing required tables in clean DB: {missing}"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_backup_and_restore_cycle_on_clean_db(tmp_path: Path) -> None:
    """Full backup, corruption/mutation, and verified restore cycle on a temporary database."""
    db_path = tmp_path / "hunter.db"
    backup_dir = tmp_path / "backups" / "clean_backup_01"

    db = Database(db_path)
    await db.connect()
    await db.migrate()
    await db.close()

    # 1. Create and verify backup
    out_dir = create_backup(db_path, backup_dir)
    assert out_dir.is_dir()
    manifest = verify_backup(backup_dir)
    assert manifest["format"] == 1
    assert manifest["user_version"] >= 19

    # 2. Mutate database with extra rows
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO users (id, telegram_user_id, chat_id, role, status, created_at) VALUES (999, 9999, 9999, 'user', 'active', '2026-10-30')")
    conn.commit()
    count_before_restore = conn.execute("SELECT COUNT(*) FROM users WHERE id=999").fetchone()[0]
    assert count_before_restore == 1
    conn.close()

    # 3. Restore backup with service_stopped=True
    retained = restore_backup(backup_dir, db_path, service_stopped=True)
    assert retained is not None and retained.is_dir()

    # 4. Verify restored state matches original backup
    conn_restored = sqlite3.connect(db_path)
    try:
        assert conn_restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn_restored.execute("PRAGMA foreign_key_check").fetchall() == []
        count_after = conn_restored.execute("SELECT COUNT(*) FROM users WHERE id=999").fetchone()[0]
        assert count_after == 0, "Mutated row must be gone after restoring clean backup"
    finally:
        conn_restored.close()


def test_restore_safety_guards(tmp_path: Path) -> None:
    """Restoration safely refuses if service is not stopped or checksums fail."""
    db_path = tmp_path / "hunter.db"
    db_path.touch()
    backup_dir = tmp_path / "backup"
    create_backup(db_path, backup_dir)

    # Must raise if service_stopped is False
    with pytest.raises(RuntimeError, match="service_stopped=True"):
        restore_backup(backup_dir, db_path, service_stopped=False)

    # Must raise if backup is corrupted
    manifest_file = backup_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["sha256"] = "corrupted_hash_value"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_backup(backup_dir, db_path, service_stopped=True)
