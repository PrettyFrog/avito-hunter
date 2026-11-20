"""Ordinary profile mutations use the same durable reference authority as recovery."""
import json
from pathlib import Path
import sqlite3

from hunter.domain.slots import SlotHealth, validate_slot_id
from hunter.storage.session_reference import decode_reference, publish_reference, read_reference
from hunter.storage.session_reference import read_reference_record as _current
from hunter.transport.session_staging import stage_session
from hunter.transport.session_store import StaleSessionVersionError
from hunter.transport.session_publication import PreparationScope


class SessionReferenceBackend:
    def __init__(self, store, writer):
        self.store, self.writer = store, writer
        self.preparation = PreparationScope()

    async def resolve(self, sid):
        manager = self.writer._conn_mgr
        reader = await manager.open_read_connection()
        try:
            return await read_reference(reader, sid)
        finally:
            await manager.close_reader(reader)

    async def save(self, sid, endpoint, version, generation, cookies, state, force):
        existing = await self.store.load_session(sid)
        if existing and not force:
            self.store._compare_version(existing.metadata, version, generation)
        prepared = self.preparation.current
        matching = prepared is not None and prepared.matches(sid, version, generation)
        if matching and prepared.candidate is not None:
            raise RuntimeError("A preparation already has a candidate")
        previous = await self.writer.submit(lambda conn: _current(conn, sid)) if matching else None
        candidate = stage_session(self.store, sid, endpoint, version, generation, cookies, state)
        from hunter.transport.session_store import SessionMetadata
        metadata = SessionMetadata(**json.loads(candidate.read_text())["metadata"])
        if matching:
            prepared.previous_reference, prepared.candidate = previous, candidate
            return metadata
        async def publish(conn):
            current = await _current(conn, sid)
            if current and (version, generation) <= (current["version"], current.get("generation", 0)):
                raise StaleSessionVersionError("A newer or equal profile is already committed")
            await conn.execute(
                "INSERT INTO runtime(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"recovery:active:{sid}", str(version)),
            )
            await publish_reference(conn, sid, version, candidate.name, generation=generation)
        # Keep a candidate on uncertain cancellation: writer.submit may still commit it.
        # An orphan is safe; unlinking a possibly committed file is not.
        await self.writer.submit(publish)
        return metadata

    async def delete(self, sid, *, expected_fence=None, slot_fence=None):
        validate_slot_id(sid)
        expected = await self.writer.submit(lambda conn: _current(conn, sid))
        existing = await self.store.load_session(sid)
        if existing is None:
            return False
        if expected_fence is not None and (existing.metadata.version, existing.metadata.generation) != expected_fence:
            return False
        version, generation = existing.metadata.version, existing.metadata.generation + 1
        async def tombstone(conn):
            if slot_fence is not None:
                slot_id, slot_generation, endpoint = slot_fence
                slot = await (await conn.execute(
                    "SELECT active_generation, endpoint, health_state FROM slots WHERE id=?", (slot_id,),
                )).fetchone()
                if (not slot or slot[0] != slot_generation or slot[2] == SlotHealth.DISABLED.value
                        or (endpoint is not None and slot[1] != endpoint)):
                    return False
            if await _current(conn, sid) != expected:
                return False
            await conn.execute(
                "INSERT INTO runtime(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"recovery:active:{sid}", str(version)),
            )
            await publish_reference(conn, sid, version, None, generation=generation, deleted=True)
            return True
        return await self.writer.submit(tombstone)

    def has_session(self, sid):
        validate_slot_id(sid)
        path = Path(self.writer._conn_mgr.config.db_path)
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT f.value,a.value FROM runtime f LEFT JOIN runtime a ON a.key=? WHERE f.key=?",
                (f"recovery:active:{sid}", f"recovery:file:{sid}"),
            ).fetchone()
        if row is None:
            return self.store._slot_path(sid).is_file()
        ref = decode_reference(sid, row[0], row[1])
        if ref.get("deleted") is True:
            return False
        candidate = self.store.base_dir / ref["file"]
        return candidate.is_file() and not candidate.is_symlink()


def prune_orphaned_session_files(base_dir: Path | str, max_age_sec: float = 300.0) -> int:
    """Removes unreferenced .staged_* files older than max_age_sec to prevent disk accumulation."""
    import time
    p = Path(base_dir)
    if not p.is_dir():
        return 0
    now_ts = time.time()
    pruned = 0
    for f in p.glob(".staged_*"):
        try:
            if f.is_file() and now_ts - f.stat().st_mtime > max_age_sec:
                f.unlink(missing_ok=True)
                pruned += 1
        except OSError:
            pass
    return pruned

