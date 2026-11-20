"""Smoke test for regression test fixtures."""

from pathlib import Path
import sqlite3


def test_clean_db_path_fixture(clean_db_path: Path) -> None:
    assert clean_db_path.suffix == ".db"
    assert clean_db_path.exists()


def test_initialized_db_fixture(initialized_db: Path) -> None:
    conn = sqlite3.connect(initialized_db)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        assert "events" in tables
        assert "deliveries" in tables
        assert "subscriptions" in tables
    finally:
        conn.close()
