"""Prepare durable, isolated session candidates without changing the active profile."""
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from tempfile import mkstemp
from typing import Any

from hunter.domain.slots import validate_slot_id
from hunter.transport.session_store import SessionMetadata, SessionStore, compute_cookie_hash


def stage_session(
    store: SessionStore, slot_id: str, endpoint: str, version: int, generation: int,
    cookies: list[dict[str, Any]], storage_state: dict[str, Any],
) -> Path:
    """Write a uniquely named candidate and fsync both the file and its directory."""
    validate_slot_id(slot_id)
    metadata = SessionMetadata(
        slot_id=slot_id, endpoint=endpoint, version=version, generation=generation,
        cookie_hash=compute_cookie_hash(cookies), updated_at=datetime.now(UTC).isoformat(),
    )
    payload = json.dumps({"metadata": metadata.to_dict(), "cookies": cookies, "storage_state": storage_state},
                         indent=2, ensure_ascii=False).encode("utf-8")
    fd, name = mkstemp(prefix=f".staged_{slot_id}_", suffix=".json", dir=store.base_dir)
    candidate = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), store.file_mode)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        directory_fd = os.open(store.base_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
    return candidate
