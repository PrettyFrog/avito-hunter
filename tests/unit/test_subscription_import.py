"""Unit tests for YAML subscription import with checkpointing (T-470)."""

from pathlib import Path
import sqlite3
import pytest
import yaml

from hunter.core.product import _TOMBSTONES
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.subscription_import import SubscriptionImporter
from hunter.storage.writer import SQLiteWriterActor

SCHEMA = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"


@pytest.fixture
async def env(tmp_path: Path):
    db_path = tmp_path / "import_test.db"
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    importer = SubscriptionImporter(mgr, writer, batch_size=2)
    _TOMBSTONES.clear()
    yield importer, writer, mgr, tmp_path
    _TOMBSTONES.clear()
    await writer.stop()
    await mgr.drain_and_close()


def _write_yaml(
    path: Path,
    pid: str,
    max_p: int = 50000,
    uid: int | None = None,
    mode: str = "live",
    flags: list[str] | None = None,
) -> Path:
    f = path / f"{pid}.yaml"
    data = {
        "id": pid,
        "name": f"Product {pid}",
        "search_url": f"https://avito.ru/moskva/{pid}",
        "max_price": max_p,
        "mode": mode,
        "owner_user_id": uid,
        "red_flags_extra": flags or ["broken"],
    }
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    return f


@pytest.mark.asyncio
async def test_full_import_and_count_hash_report(env) -> None:
    importer, _, mgr, tmp_path = env
    p_dir = tmp_path / "profiles"
    p_dir.mkdir()
    _write_yaml(p_dir, "prod_a", 40000, uid=101)
    _write_yaml(p_dir, "prod_b", 60000, uid=202)

    rep = await importer.import_from_directory(p_dir)
    assert rep.imported_count == 2 and rep.skipped_count == 0 and rep.error_count == 0
    assert len(rep.profiles_hash) == 16 and rep.checkpoint_cursor == "prod_b"

    reader = await mgr.open_read_connection()
    cur = await reader.execute(
        "SELECT id, user_id, max_price, enabled FROM subscriptions ORDER BY id"
    )
    rows = await cur.fetchall()
    assert len(rows) == 2
    # User isolation preserved
    assert rows[0][0] == "prod_a" and rows[0][1] == 101 and rows[0][2] == 40000
    assert rows[1][0] == "prod_b" and rows[1][1] == 202 and rows[1][2] == 60000

    # Quiet baseline
    q_cur = await reader.execute("SELECT bootstrap_state FROM queries WHERE id = 'q_prod_a'")
    assert (await q_cur.fetchone())[0] == "ready"
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_interrupted_import_and_resume_checkpoint(env) -> None:
    importer, _, mgr, tmp_path = env
    p_dir = tmp_path / "profiles_resume"
    p_dir.mkdir()
    for name in ("p1", "p2", "p3", "p4"):
        _write_yaml(p_dir, name)

    # Simulate checkpoint at p2
    await importer.set_checkpoint("p2")
    rep = await importer.import_from_directory(p_dir, resume=True)

    # p1 and p2 skipped, p3 and p4 imported
    assert rep.imported_count == 2 and rep.skipped_count == 2
    assert rep.checkpoint_cursor == "p4"

    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT id FROM subscriptions ORDER BY id")
    ids = [r[0] for r in await cur.fetchall()]
    assert ids == ["p3", "p4"]
    await mgr.close_reader(reader)


@pytest.mark.asyncio
async def test_invalid_profile_and_tombstoned_deleted(env) -> None:
    importer, _, mgr, tmp_path = env
    p_dir = tmp_path / "profiles_err"
    p_dir.mkdir()

    _write_yaml(p_dir, "p_valid")
    # Invalid profile
    (p_dir / "p_bad.yaml").write_text("invalid_yaml: [missing bracket", encoding="utf-8")
    # Tombstoned profile
    _write_yaml(p_dir, "p_tomb")
    _TOMBSTONES.add("p_tomb")

    rep = await importer.import_from_directory(p_dir)
    assert rep.imported_count == 1 and rep.skipped_count == 1 and rep.error_count == 1
    assert any("p_bad.yaml" in e for e in rep.errors)

    reader = await mgr.open_read_connection()
    cur = await reader.execute("SELECT id FROM subscriptions")
    assert [r[0] for r in await cur.fetchall()] == ["p_valid"]
    await mgr.close_reader(reader)
