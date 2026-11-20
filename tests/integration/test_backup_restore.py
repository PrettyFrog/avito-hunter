from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ops.backup import create_backup, restore_backup, verify_backup


def _database(path: Path, values: tuple[str, ...]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA user_version=19")
        conn.execute("CREATE TABLE IF NOT EXISTS item (value TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO item VALUES (?)", ((v,) for v in values))


def test_backup_captures_committed_wal_and_verifies_checksums(tmp_path: Path) -> None:
    source = tmp_path / "hunter.db"
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA user_version=19")
    conn.execute("CREATE TABLE item (value TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO item VALUES ('wal-committed')")
    conn.commit()

    snapshot = create_backup(source, tmp_path / "snapshot")
    manifest = verify_backup(snapshot)
    with sqlite3.connect(snapshot / "database.sqlite3") as backup:
        assert backup.execute("SELECT value FROM item").fetchone()[0] == "wal-committed"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    assert manifest["user_version"] == 19
    conn.close()


def test_restore_preserves_newer_rows_in_a_verified_pre_restore_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hunter.db"
    _database(source, ("before",))
    snapshot = create_backup(source, tmp_path / "snapshot")
    _database(source, ("new-after-snapshot",))

    with pytest.raises(RuntimeError, match="stop the service"):
        restore_backup(snapshot, source)
    restore_backup(snapshot, source, service_stopped=True)
    with sqlite3.connect(source) as restored:
        assert restored.execute("SELECT value FROM item").fetchall() == [("before",)]
    saved = next(tmp_path.glob("hunter.db.pre-restore-*"))
    verify_backup(saved)
    with sqlite3.connect(saved / "database.sqlite3") as current:
        assert current.execute("SELECT value FROM item WHERE value='new-after-snapshot'").fetchone()


def test_fault_interrupted_backup_does_not_publish_partial_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "hunter.db"
    _database(source, tuple(f"row-{n}" for n in range(5000)))
    target = tmp_path / "interrupted"

    def interrupt_after_progress(status: int, remaining: int, total: int) -> None:
        if total - remaining > 1:
            raise RuntimeError("injected backup interruption")

    with pytest.raises(RuntimeError, match="injected backup interruption"):
        create_backup(source, target, progress=interrupt_after_progress)
    assert not target.exists()
    assert not list(tmp_path.glob(".interrupted.*.tmp"))


def test_fault_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "hunter.db"
    _database(source, ("row",))
    snapshot = create_backup(source, tmp_path / "snapshot")
    database = snapshot / "database.sqlite3"
    with database.open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_backup(snapshot)
