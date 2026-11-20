"""Hermetic test fixtures, sandbox settings, and fixed clock (S0)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunter.core.config import Settings
from hunter.core.db import Database


class FixedClock:
    """Deterministic clock for hermetic tests."""

    def __init__(self, start_time: datetime | None = None) -> None:
        if start_time is None:
            start_time = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
        self._current = start_time

    def now_utc(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> datetime:
        from datetime import timedelta

        self._current += timedelta(seconds=seconds)
        return self._current

    def timestamp(self) -> float:
        return self._current.timestamp()

    def isoformat(self) -> str:
        return self._current.isoformat()


async def create_sandbox_db(db_path: Path) -> Database:
    """Create a fully initialized, hermetic synthetic SQLite database."""
    if db_path.is_dir():
        db_file = db_path / "sandbox.db"
    else:
        db_file = db_path
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_file)
    await db.connect()
    return db


def backup_sqlite_db(src: Path, dest: Path) -> Path:
    """Transactionally copy SQLite database including active WAL contents."""
    import sqlite3

    dest.parent.mkdir(parents=True, exist_ok=True)
    src_uri = f"file:{src.resolve()}?mode=ro"
    with sqlite3.connect(src_uri, uri=True) as s_conn, sqlite3.connect(dest) as d_conn:
        s_conn.backup(d_conn)
    return dest


def create_sandbox_settings(base_dir: Path) -> Settings:
    """Create isolated settings pointing exclusively to a temporary sandbox."""
    data_dir = base_dir / "data"
    products_dir = data_dir / "products"
    raw_dir = data_dir / "raw"
    for d in [data_dir, products_dir, raw_dir]:
        d.mkdir(parents=True, exist_ok=True)

    return Settings(
        telegram_token="",
        owner_chat_id=12345678,
        data_dir=data_dir,
        gemini_api_key="",
        deepseek_api_key="",
        proxy_url=None,
    )


def get_synthetic_parity_profiles() -> dict[str, Any]:
    """Load synthetic profiles without reading data/products."""
    import json

    from hunter.core.product import ProductProfile

    p_file = Path(__file__).resolve().parent / "synthetic_profiles.json"
    data = json.loads(p_file.read_text(encoding="utf-8"))
    return {pid: ProductProfile(**pdata) for pid, pdata in data.items()}


def create_synthetic_v1_db(db_path: Path) -> Path:
    """Create a synthetic hermetic SQLite database at schema v1 with sample rows."""
    import json
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    schema_file = Path(__file__).resolve().parent / "synthetic_schemas.json"
    schemas = json.loads(schema_file.read_text(encoding="utf-8"))
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(schemas["v1_ddl"])
    cur.execute("PRAGMA user_version = 1")
    cur.execute("INSERT INTO metrics(day, requests) VALUES ('2026-10-23', 10)")
    cur.execute(
        "INSERT INTO listings(avito_id, url, first_seen, last_seen, status, title, price) "
        "VALUES ('synth_lot_1', 'https://avito.ru/1', "
        "'2026-10-23T00:00:00Z', '2026-10-23T00:00:00Z', 'new', 'Phone', 10000)"
    )
    cur.execute(
        "INSERT INTO notifications(avito_id, channel, idempotency_key, sent_at) "
        "VALUES ('synth_lot_1', 'telegram', 'idem_1', '2026-10-23T00:01:00Z')"
    )
    cur.execute("INSERT INTO products(model_id, category) VALUES ('synth_prod_1', 'electronics')")
    con.commit()
    con.close()
    return db_path


def create_synthetic_v6_db(db_path: Path) -> Path:
    """Create a synthetic hermetic SQLite database at schema v6 with sample rows."""
    import json
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    schema_file = Path(__file__).resolve().parent / "synthetic_schemas.json"
    schemas = json.loads(schema_file.read_text(encoding="utf-8"))
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(schemas["v6_ddl"])
    cur.execute("PRAGMA user_version = 6")
    cur.execute("INSERT INTO metrics(day, requests) VALUES ('2026-10-23', 10)")
    cur.execute(
        "INSERT INTO listings(avito_id, url, first_seen, last_seen, status, title, price) "
        "VALUES ('synth_lot_1', 'https://avito.ru/1', "
        "'2026-10-23T00:00:00Z', '2026-10-23T00:00:00Z', 'new', 'Phone', 10000)"
    )
    cur.execute(
        "INSERT INTO notifications(avito_id, channel, idempotency_key, sent_at) "
        "VALUES ('synth_lot_1', 'telegram', 'idem_1', '2026-10-23T00:01:00Z')"
    )
    cur.execute("INSERT INTO feedback(avito_id, feedback) VALUES ('synth_lot_1', 'like')")
    cur.execute("INSERT INTO products(id, category) VALUES ('synth_prod_1', 'electronics')")
    con.commit()
    con.close()
    return db_path
