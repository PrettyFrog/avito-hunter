"""Common helpers for parity snapshot recording and validation."""

from __future__ import annotations

import gzip
import json
import os
import shutil
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def get_project_root() -> Path:
    return ROOT_DIR


def copy_temp_db(source_rel_path: str = "data/hunter.db", dest_dir: Path | None = None) -> Path:
    """Safely and transactionally copy the database to a unique temp file using sqlite3 backup."""
    import sqlite3
    import uuid

    src = ROOT_DIR / source_rel_path
    if not src.exists():
        src = ROOT_DIR / "data/prod_snapshot.db"

    unique_name = f"hunter_parity_{uuid.uuid4().hex[:8]}.db"
    dest = (dest_dir or Path("/tmp")) / unique_name
    dest.parent.mkdir(parents=True, exist_ok=True)

    if src.exists():
        src_uri = f"file:{src.resolve()}?mode=ro"
        with sqlite3.connect(src_uri, uri=True) as src_conn, sqlite3.connect(dest) as dest_conn:
            src_conn.backup(dest_conn)
    else:
        dest.touch()

    return dest


def cleanup_temp_db(path: Path) -> None:
    """Remove temporary database copy from /tmp."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def load_fixture_text(rel_path: str) -> str:
    """Load text fixture supporting both plain and .gz compressed files."""
    full_path = ROOT_DIR / rel_path
    if not full_path.exists():
        raise FileNotFoundError(f"Fixture not found: {full_path}")
    if full_path.suffix == ".gz":
        with gzip.open(full_path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    return full_path.read_text(encoding="utf-8", errors="replace")


def dump_json_snapshot(data: Any, dest_path: Path) -> None:
    """Serialize data to formatted deterministic JSON."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    dest_path.write_text(content + "\n", encoding="utf-8")


def load_json_snapshot(src_path: Path) -> Any:
    """Load JSON snapshot."""
    return json.loads(src_path.read_text(encoding="utf-8"))
