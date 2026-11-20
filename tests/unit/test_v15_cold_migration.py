"""Cold migration must not depend on pytest collection importing migrations."""

import json
import os
from pathlib import Path
import subprocess
import sys

from hunter.storage.migration_runner import LATEST_VERSION


def test_cold_start_registers_complete_schema(tmp_path: Path) -> None:
    script = """
import asyncio
import json
import sys
from hunter.core.db import Database

async def main():
    db = Database(sys.argv[1])
    await db.connect()
    await db.migrate()
    conn = await db.connect()
    version = (await (await conn.execute('PRAGMA user_version')).fetchone())[0]
    columns = [row[1] for row in await (await conn.execute('PRAGMA table_info(jobs)')).fetchall()]
    table_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    table_rows = await table_cursor.fetchall()
    tables = [row[0] for row in table_rows]
    acknowledged = False
    if version >= 15 and 'lease_token' in columns:
        job_id = await db.enqueue_job('cold_probe', 'product', 'cold_probe_1', {})
        lease = await db.lease_job('cold_probe')
        acknowledged = await db.complete_job(job_id, lease_token=lease['lease_token'])
    await db.close()
    restarted = Database(sys.argv[1])
    await restarted.connect()
    restart_conn = await restarted.connect()
    restart_version = (await (await restart_conn.execute('PRAGMA user_version')).fetchone())[0]
    await restarted.close()
    print(json.dumps({'version': version, 'restart_version': restart_version,
                      'columns': columns, 'tables': tables, 'acknowledged': acknowledged}))

asyncio.run(main())
"""
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "cold.db")],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
        timeout=15,
    )
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["version"] == LATEST_VERSION
    assert state["restart_version"] == LATEST_VERSION
    assert "lease_token" in state["columns"]
    assert {"slots", "query_groups", "price_watch_jobs"} <= set(state["tables"])
    assert state["acknowledged"] is True
