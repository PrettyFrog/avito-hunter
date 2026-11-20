from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/install_xeon.sh"
UNIT = ROOT / "ops/systemd/avito-hunter.service"


def test_service_unit_is_headless_and_runs_as_dedicated_service_user() -> None:
    unit = UNIT.read_text()

    assert "User=avito-hunter" in unit
    assert "Group=avito-hunter" in unit
    assert "EnvironmentFile=/etc/avito-hunter/avito-hunter.env" in unit
    assert "HUNTER_DATA_DIR=/var/lib/avito-hunter" in unit
    assert "Restart=on-failure" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "avito-xvfb" not in unit
    assert "DISPLAY=" not in unit


def test_fake_staging_install_creates_empty_latest_schema_without_legacy_seed(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    env = os.environ.copy()
    env["HUNTER_XEON_PYTHON"] = str(ROOT / ".venv/bin/python")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--staging-root", str(stage)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    db_path = stage / "var/lib/avito-hunter/hunter.db"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 19
        assert conn.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 0

    marker = "install-sentinel"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS install_sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO install_sentinel VALUES (?)", (marker,))

    env_file = stage / "etc/avito-hunter/avito-hunter.env"
    env_file.write_text("PRESERVE_SENTINEL=keep-me\n")
    rerun = subprocess.run(
        ["bash", str(SCRIPT), "--staging-root", str(stage)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert env_file.read_text() == "PRESERVE_SENTINEL=keep-me\n"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT value FROM install_sentinel").fetchone()[0] == marker

    assert "seed-from-legacy" not in SCRIPT.read_text()
    assert (stage / "etc/systemd/system/avito-hunter.service").is_file()
    assert (stage / "etc/avito-hunter/avito-hunter.env").is_file()
