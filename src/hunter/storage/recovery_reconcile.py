"""Reconcile startup session state through the owned SQLite writer."""
from pathlib import Path
from typing import Any

from hunter.storage.recovery_commit import _restore_receipt_file
from hunter.storage.recovery_receipts import active_versions, receipt_is_obsolete


async def _active_versions(conn: Any) -> dict[str, int]:
    cursor = await conn.execute("SELECT key, value FROM runtime WHERE key LIKE 'recovery:active:%'")
    return active_versions(await cursor.fetchall())


async def _ready_versioned_slots(conn: Any) -> list[str]:
    cursor = await conn.execute(
        "SELECT s.id FROM slots s JOIN session_versions v ON v.slot_id = s.id "
        "WHERE s.health_state = 'READY' AND v.version > 1"
    )
    return [row[0] for row in await cursor.fetchall()]


def _quarantine_slots(slot_ids: set[str]) -> Any:
    async def apply(conn: Any) -> None:
        for slot_id in slot_ids:
            await conn.execute(
                "UPDATE slots SET health_state = 'QUARANTINED', "
                "quarantine_until = datetime('now', '+60 seconds'), updated_at = datetime('now') "
                "WHERE id = ? AND health_state = 'READY'", (slot_id,),
            )
    return apply


async def reconcile_owned_sessions(store: Any, writer: Any, db_path: Path | str) -> list[str]:
    """Keep disk operations outside writer transactions and retain receipts until commit."""
    owned_db = Path(db_path)
    if not owned_db.is_file():
        raise FileNotFoundError(f"Owned recovery database is missing: {owned_db}")
    if owned_db.resolve() != Path(writer._conn_mgr.config.db_path).resolve():
        raise ValueError("Recovery database does not match writer database")
    base = Path(store.base_dir)
    if not base.exists():
        return []
    receipts = list(base.glob(".tmp_slot_*"))
    versions = await writer.submit(_active_versions)
    uncommitted = {
        _restore_receipt_file(base, path) for path in receipts if not receipt_is_obsolete(path, base, versions)
    }
    ready_slots = await writer.submit(_ready_versioned_slots)
    missing = {slot for slot in ready_slots if not (store.has_session(slot) or store.has_session(f"{slot}__user_b"))}
    await writer.submit(_quarantine_slots(uncommitted | missing))
    for path in receipts:
        path.unlink(missing_ok=True)
    return [path.name for path in receipts]


async def reconcile_startup_state(store: Any, writer: Any, db_path: Path | str | None) -> list[str]:
    """Coordinates session reconciliation and query f-param reconciliation on startup."""
    res = []
    if store and writer and db_path:
        res = await reconcile_owned_sessions(store, writer, db_path)
    if writer:
        from hunter.storage.query_f_reconcile import reconcile_conflicting_f_queries
        await reconcile_conflicting_f_queries(writer)
        from hunter.storage.subscriptions import reconcile_stale_assignments
        await reconcile_stale_assignments(writer)
    return res

