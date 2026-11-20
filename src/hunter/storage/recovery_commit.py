"""Single-writer guarded commit for slot challenge recovery (T-975)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from hunter.domain.slots import SlotHealth, SlotSnapshot
from hunter.storage.slot_state import SlotRepository
from hunter.storage.recovery_activation import _build_activation_tx
from hunter.transport.recovery_identity import resolve_session_slot_id
from hunter.transport.session_store import SessionStore

logger = logging.getLogger(__name__)


async def _validate_slot_precommit(
    slot_repo: SlotRepository, slot_id: str, exp_gen: int, target_endpoint: str | None
) -> SlotSnapshot | None:
    cur = await slot_repo.get_slot(slot_id)
    if not cur or cur.health == SlotHealth.DISABLED or cur.generation != exp_gen:
        return None
    if target_endpoint and cur.endpoint != target_endpoint:
        return None
    return cur


def _build_cas_reserve_tx(
    slot_id: str, exp_gen: int, target_endpoint: str | None,
    query_id: str | None, asg_gen: int | None, cookie_hash: str,
) -> Any:
    async def _tx(c: Any) -> tuple[bool, int]:
        row = await (await c.execute(
            "SELECT health_state, active_generation, endpoint FROM slots WHERE id = ?", (slot_id,)
        )).fetchone()
        if not row or row[0] == SlotHealth.DISABLED.value or int(row[1]) != exp_gen:
            return False, 0
        if target_endpoint and row[2] != target_endpoint:
            return False, 0
        if query_id is not None and asg_gen is not None:
            asg = await (await c.execute(
                "SELECT 1 FROM assignments WHERE slot_id = ? AND query_id = ? AND generation = ? AND active = 1",
                (slot_id, query_id, int(asg_gen)),
            )).fetchone()
            if not asg:
                return False, 0
        now_ts = datetime.now(UTC).isoformat()
        await c.execute(
            "INSERT INTO session_versions (slot_id, version, cookie_hash, updated_at) VALUES (?, 1, ?, ?) "
            "ON CONFLICT(slot_id) DO UPDATE SET version = session_versions.version + 1, "
            "cookie_hash = CASE WHEN excluded.cookie_hash != '' THEN excluded.cookie_hash ELSE session_versions.cookie_hash END, "
            "updated_at = excluded.updated_at",
            (slot_id, cookie_hash, now_ts),
        )
        cur = await c.execute("SELECT version FROM session_versions WHERE slot_id = ?", (slot_id,))
        v_row = await cur.fetchone()
        return True, int(v_row[0]) if v_row else 1
    return _tx


def _build_compensation_tx(slot_id: str, exp_gen: int, expected_version: int | None = None) -> Any:
    async def _tx(c: Any) -> bool:
        now_ts = datetime.now(UTC).isoformat()
        query = (
            "UPDATE slots SET health_state = ?, quarantine_until = datetime('now', '+60 seconds'), updated_at = ? "
            "WHERE id = ? AND active_generation = ? AND health_state != ?"
        )
        params: list[Any] = [SlotHealth.QUARANTINED.value, now_ts, slot_id, exp_gen, SlotHealth.DISABLED.value]
        if expected_version is not None:
            query += " AND (SELECT version FROM session_versions WHERE slot_id = ?) <= ?"
            params.extend([slot_id, expected_version])
        cur = await c.execute(query, tuple(params))
        return cur.rowcount > 0
    return _tx


def _read_disk_version(dest_path: Path) -> int:
    try:
        content = dest_path.read_text(encoding="utf-8")
        meta = json.loads(content).get("metadata", {})
        return int(meta.get("version", 0))
    except Exception:
        return 0


async def _restore_or_cleanup(
    session_store: SessionStore, slot_id: str, dest_path: Any,
    prev_bytes: bytes | None, new_version: int, exp_gen: int,
) -> None:
    if isinstance(dest_path, Path) and dest_path.exists():
        if _read_disk_version(dest_path) != new_version:
            return
    if isinstance(dest_path, Path) and prev_bytes is not None:
        from tempfile import NamedTemporaryFile
        import os
        with NamedTemporaryFile(mode="wb", dir=dest_path.parent, prefix=".tmp_restore_", delete=False) as temp:
            t_path = Path(temp.name)
            try:
                temp.write(prev_bytes)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(t_path, dest_path)
            finally:
                t_path.unlink(missing_ok=True)
    else:
        await _cleanup_uncommitted_file(session_store, slot_id, new_version, exp_gen)


async def _cleanup_uncommitted_file(session_store: SessionStore, slot_id: str, new_version: int, exp_gen: int) -> None:
    dest_path = session_store._slot_path(slot_id) if hasattr(session_store, "_slot_path") else None
    if not isinstance(dest_path, Path) or not dest_path.exists():
        return
    data = json.loads(dest_path.read_text(encoding="utf-8"))
    meta = data.get("metadata", {})
    f_ver = int(meta.get("version", 0))
    f_gen = int(meta.get("generation", 0))
    if f_ver <= new_version and f_gen <= exp_gen:
        await session_store.delete_session(slot_id)


async def _extract_storage_state(
    store: SessionStore, sid: str, ep: str | None, page: Any,
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if page and hasattr(page, "evaluate"):
        with suppress(Exception): state["user_agent"] = await page.evaluate("() => navigator.userAgent")
        with suppress(Exception): state["accept_language"] = f"{await page.evaluate('() => navigator.language')},ru;q=0.8"
    else:
        with suppress(Exception):
            ex = await store.load_session(sid, expected_endpoint=ep or "")
            if ex and isinstance(getattr(ex, "storage_state", None), dict):
                state = dict(ex.storage_state)
    return state


def _restore_receipt_file(base_p: Path, p: Path) -> str:
    slot_name = p.name[len(".tmp_slot_"):].split(".")[0].split("__")[0]
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid recovery receipt: {p.name}")
    slot_name = data.get("slot_id") or slot_name
    sid = data.get("session_slot_id") or slot_name
    target = base_p / f"slot_{sid}_session.json"
    if data.get("prev_bytes"):
        import base64
        from tempfile import NamedTemporaryFile
        import os
        raw = base64.b64decode(data["prev_bytes"].encode("ascii"), validate=True)
        with NamedTemporaryFile(mode="wb", dir=base_p, prefix=".tmp_rc_", delete=False) as temp:
            t_p = Path(temp.name)
            try:
                temp.write(raw)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(t_p, target)
            finally:
                t_p.unlink(missing_ok=True)
    elif target.exists():
        target.unlink(missing_ok=True)
    return slot_name


def _reconcile_database_slots(db_path: Path, uncommitted: set[str], store: SessionStore) -> None:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='slots'")
        if not cur.fetchone():
            return
        for s_id in uncommitted:
            cur.execute(
                "UPDATE slots SET health_state = 'QUARANTINED', quarantine_until = datetime('now', '+60 seconds'), "
                "updated_at = datetime('now') WHERE id = ? AND health_state = 'READY'",
                (s_id,),
            )
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_versions'")
        if cur.fetchone():
            cur.execute(
                "SELECT s.id FROM slots s JOIN session_versions v ON v.slot_id = s.id "
                "WHERE s.health_state = 'READY' AND v.version > 1"
            )
            for (s_id,) in cur.fetchall():
                if not (store.has_session(s_id) or store.has_session(f"{s_id}__user_b")):
                    cur.execute(
                        "UPDATE slots SET health_state = 'QUARANTINED', quarantine_until = datetime('now', '+60 seconds'), "
                        "updated_at = datetime('now') WHERE id = ? AND health_state = 'READY'",
                        (s_id,),
                    )
        conn.commit()


def reconcile_uncommitted_receipts(session_store: SessionStore, db_path: Path | str | None = None) -> list[str]:
    """Cleans up orphaned recovery receipt temp files and reconciles uncommitted slots."""
    from hunter.transport.session_ownership import resolve_session_owner
    owned_db = resolve_session_owner(session_store.base_dir, db_path)
    if not owned_db.is_file():
        raise FileNotFoundError(f"Owned recovery database is missing: {owned_db}")
    cleaned: list[str] = []
    base = getattr(session_store, "base_dir", None)
    if not base or not Path(base).exists():
        return cleaned
    base_p = Path(base)
    uncommitted: set[str] = set()
    from hunter.storage.recovery_receipts import active_versions, receipt_is_obsolete
    import sqlite3
    with sqlite3.connect(owned_db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        versions = active_versions(conn.execute("SELECT key, value FROM runtime WHERE key LIKE 'recovery:active:%'"))
    for p in base_p.glob(".tmp_slot_*"):
        if not receipt_is_obsolete(p, base_p, versions):
            slot_name = _restore_receipt_file(base_p, p)
            if slot_name:
                uncommitted.add(slot_name)
        with suppress(Exception):
            p.unlink(missing_ok=True)
            cleaned.append(p.name)

    _reconcile_database_slots(owned_db, uncommitted, session_store)
    return cleaned


def _write_uncommitted_receipt(
    store: SessionStore, slot_id: str, session_slot_id: str,
    version: int, exp_gen: int, prev_bytes: bytes | None,
) -> Path:
    base = getattr(store, "base_dir", None)
    if not isinstance(base, (str, Path)) or not Path(base).is_dir():
        raise FileNotFoundError("Session receipt directory is unavailable")
    import base64
    from tempfile import NamedTemporaryFile
    import os
    rcpt = Path(base) / f".tmp_slot_{session_slot_id}__uncommitted_{version}.json"
    payload = {
        "slot_id": slot_id,
        "session_slot_id": session_slot_id,
        "version": version,
        "generation": exp_gen,
        "prev_bytes": base64.b64encode(prev_bytes).decode("ascii") if prev_bytes else None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    temp = NamedTemporaryFile(mode="w", dir=base, prefix=".tmp_rc_", delete=False, encoding="utf-8")
    t_p = Path(temp.name)
    try:
        temp.write(json.dumps(payload))
        temp.flush()
        os.fsync(temp.fileno())
        temp.close()
        os.replace(t_p, rcpt)
        dir_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return rcpt
    except Exception:
        with suppress(Exception):
            temp.close()
            t_p.unlink(missing_ok=True)
        raise


def _remove_receipt(rcpt: Path | None) -> None:
    if rcpt and isinstance(rcpt, Path):
        with suppress(Exception):
            rcpt.unlink(missing_ok=True)


async def _compensate_error(
    slot_repo: SlotRepository, slot_id: str, exp_gen: int, new_version: int,
) -> None:
    with suppress(Exception):
        cur = await slot_repo.get_slot(slot_id)
        if cur and cur.generation == exp_gen:
            await slot_repo.writer.submit(
                _build_compensation_tx(slot_id, exp_gen, expected_version=new_version)
            )


async def _prepare_receipt(
    repo: SlotRepository, store: SessionStore, slot_id: str, sid: str,
    version: int, generation: int, previous: bytes | None,
) -> Path:
    try:
        return _write_uncommitted_receipt(store, slot_id, sid, version, generation, previous)
    except Exception:
        await _compensate_error(repo, slot_id, generation, version)
        raise


async def _activate_and_verify(
    slot_repo: SlotRepository, slot_id: str, exp_gen: int, target_ep: str | None,
    ver: int, q_id: str | None, asg_gen: int | None,
    session_slot_id: str | None = None,
    prepared: Any = None, session_store: SessionStore | None = None,
) -> bool:
    if prepared is not None and prepared.candidate is None:
        return False
    act_tx = _build_activation_tx(slot_id, exp_gen, target_ep, ver, q_id, asg_gen, session_slot_id,
                                 prepared.candidate.name if prepared is not None else None, prepared)
    if not await slot_repo.writer.submit(act_tx):
        return False
    cur = await slot_repo.get_slot(slot_id)
    valid = bool(cur and cur.health != SlotHealth.DISABLED and cur.generation == exp_gen)
    if prepared is not None and valid:
        from hunter.storage.session_reference import read_reference
        reference = await slot_repo.writer.submit(lambda c: read_reference(c, prepared.slot_id))
        valid = reference == (ver, prepared.candidate.name)
        if valid and session_store is not None:
            from hunter.transport.session_publication import project_legacy_profile
            project_legacy_profile(session_store, prepared)
    return valid


async def guarded_recovery_commit(
    slot_repo: SlotRepository, session_store: SessionStore, slot: SlotSnapshot, cookies: list[dict[str, Any]],
    storage_state: dict[str, Any] | None = None, page: Any = None, client_identity: str = "",
    expected_endpoint: str | None = None, expected_generation: int | None = None,
    query_id: str | None = None, assignment_generation: int | None = None,
) -> int | None:
    """Guarded commit for challenge recovery under single writer."""
    target_endpoint = expected_endpoint or slot.endpoint
    exp_gen = expected_generation if expected_generation is not None else slot.generation
    if not (current_slot := await _validate_slot_precommit(slot_repo, slot.slot_id, exp_gen, target_endpoint)):
        return None
    session_slot_id = resolve_session_slot_id(slot.slot_id, client_identity)
    if storage_state is None:
        storage_state = await _extract_storage_state(session_store, session_slot_id, target_endpoint, page)
    cookie_hash = hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).hexdigest() if cookies else ""

    tx = _build_cas_reserve_tx(slot.slot_id, exp_gen, target_endpoint, query_id, assignment_generation, cookie_hash)
    res = await slot_repo.writer.submit(tx)
    committed, new_version = (
        res if isinstance(res, tuple) and len(res) == 2
        else (bool(res), current_slot.session_version + 1)
    )
    if not committed:
        return None

    dest_path = session_store._slot_path(session_slot_id) if hasattr(session_store, "_slot_path") else None
    prev_bytes = dest_path.read_bytes() if isinstance(dest_path, Path) and dest_path.exists() else None
    rcpt = await _prepare_receipt(
        slot_repo, session_store, slot.slot_id, session_slot_id, new_version, exp_gen, prev_bytes
    )
    try:
        from hunter.transport.session_store import StaleSessionVersionError
        try:
            with session_store.prepare_publication(slot_repo.writer, session_slot_id, new_version, exp_gen) as prepared:
                await session_store.save_session(
                    slot_id=session_slot_id, endpoint=target_endpoint or "", version=new_version,
                    generation=exp_gen, cookies=cookies, storage_state=storage_state, force=False,
                )
        except StaleSessionVersionError:
            logger.info("Session store has newer version than %s; aborting commit", new_version)
            await _restore_or_cleanup(session_store, session_slot_id, dest_path, prev_bytes, new_version, exp_gen)
            _remove_receipt(rcpt)
            return None
    except (Exception, asyncio.CancelledError):
        await _compensate_error(slot_repo, slot.slot_id, exp_gen, new_version)
        await _restore_or_cleanup(session_store, session_slot_id, dest_path, prev_bytes, new_version, exp_gen)
        _remove_receipt(rcpt)
        raise

    if not await _activate_and_verify(
        slot_repo, slot.slot_id, exp_gen, target_endpoint, new_version, query_id, assignment_generation, session_slot_id,
        prepared, session_store,
    ):
        await _restore_or_cleanup(session_store, session_slot_id, dest_path, prev_bytes, new_version, exp_gen)
        _remove_receipt(rcpt)
        return None

    _remove_receipt(rcpt)
    return new_version
