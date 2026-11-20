"""Transactional references from a session identity to its durable candidate."""
import json
import re
from typing import Any

from hunter.domain.slots import validate_slot_id


def validate_candidate_name(slot_id: str, filename: str) -> None:
    validate_slot_id(slot_id)
    if not isinstance(filename, str) or not re.fullmatch(
        rf"\.staged_{re.escape(slot_id)}_[a-z0-9_]{{8}}\.json", filename
    ):
        raise ValueError("Candidate filename does not belong to session identity")


async def publish_reference(conn: Any, slot_id: str, version: int, filename: str | None,
                            *, generation: int = 0, deleted: bool = False) -> None:
    """Publish within the caller's activation transaction after version fencing."""
    if not deleted:
        validate_candidate_name(slot_id, filename)
    elif filename is not None:
        raise ValueError("Deleted profile must not reference a candidate")
    cursor = await conn.execute("SELECT value FROM runtime WHERE key = ?", (f"recovery:active:{slot_id}",))
    row = await cursor.fetchone()
    if row is None or int(row[0]) != version:
        raise ValueError("Candidate version is not the active fenced version")
    payload = {"version": version, "file": filename}
    if generation:
        payload["generation"] = generation
    if deleted:
        payload["deleted"] = True
    reference = json.dumps(payload, separators=(",", ":"))
    await conn.execute(
        "INSERT INTO runtime(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"recovery:file:{slot_id}", reference),
    )


def decode_reference(slot_id: str, value: str, active: str | None) -> dict[str, Any]:
    ref = json.loads(value)
    if not isinstance(ref, dict) or type(ref.get("version")) is not int:
        raise ValueError("Invalid session reference")
    if active is None or int(active) != ref["version"]:
        raise ValueError("Session reference does not match active version")
    if not (ref.get("deleted") is True and ref.get("file") is None):
        validate_candidate_name(slot_id, ref.get("file"))
    return ref


async def read_reference_record(conn: Any, slot_id: str) -> dict[str, Any] | None:
    validate_slot_id(slot_id)
    cursor = await conn.execute(
        "SELECT f.value, a.value FROM runtime f LEFT JOIN runtime a ON a.key = ? WHERE f.key = ?",
        (f"recovery:active:{slot_id}", f"recovery:file:{slot_id}"),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return decode_reference(slot_id, row[0], row[1])


async def read_reference(conn: Any, slot_id: str) -> tuple[int, str | None] | None:
    """Return a validated reference only when its active version agrees."""
    ref = await read_reference_record(conn, slot_id)
    return (ref["version"], ref["file"]) if ref is not None else None
