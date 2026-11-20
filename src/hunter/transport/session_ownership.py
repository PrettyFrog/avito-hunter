"""Persist an explicitly configured database owner; never discover neighboring DBs."""
import json
import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import sqlite3


def _identity(path: Path) -> dict:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Session owner database is not a regular file")
    stat = path.stat()
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}


def _read_binding(base: Path) -> dict:
    marker = base / ".session_owner.json"
    if marker.is_symlink():
        raise ValueError("Session owner binding must not be a symlink")
    binding = json.loads(marker.read_text())
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
        raise ValueError("Invalid session owner binding")
    if not Path(binding["path"]).is_absolute():
        raise ValueError("Session owner path must be absolute")
    if any(type(binding.get(key)) is not int for key in ("device", "inode")):
        raise ValueError("Invalid database identity")
    return binding


def bind_session_owner(base: Path, db_path: Path) -> None:
    """Only call using the actual owner writer/config, never inferred from a receipt."""
    binding = _identity(db_path)
    marker = base / ".session_owner.json"
    if marker.exists() or marker.is_symlink():
        previous = _read_binding(base)
        if previous["path"] != binding["path"]:
            raise ValueError("Session directory is owned by another database")
        if previous == binding:
            return
    with NamedTemporaryFile(mode="w", dir=base, prefix=".owner_", delete=False) as output:
        temp = Path(output.name)
        try:
            os.fchmod(output.fileno(), 0o600)
            json.dump(binding, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, marker)
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def resolve_session_owner(base: Path, explicit_path: Path | str | None = None) -> Path:
    if explicit_path is not None:
        owned = Path(explicit_path).resolve(strict=True)
        if not (base / ".session_owner.json").exists():
            _identity(owned)
            return owned
    binding = _read_binding(base)
    owned = Path(binding["path"])
    if explicit_path is not None and owned != Path(explicit_path).resolve(strict=True):
        raise ValueError("Explicit recovery database differs from session owner")
    if _identity(owned) != binding:
        raise ValueError("Recovery database identity changed")
    return owned


def owned_session_directory(base: Path, db_path: Path) -> Path:
    """Multiple explicitly configured DB owners must not share profile authority."""
    marker = base / ".session_owner.json"
    if marker.exists() or marker.is_symlink():
        binding = _read_binding(base)
        if binding["path"] != str(db_path.resolve(strict=True)):
            namespace = hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()[:24]
            return base / "owners" / namespace
    return base


def read_owned_reference(base: Path, slot_id: str) -> tuple[int, str | None] | None:
    """Read committed authority without creating a writer or guessing a DB path."""
    from hunter.domain.slots import validate_slot_id
    from hunter.storage.session_reference import decode_reference
    validate_slot_id(slot_id)
    marker = base / ".session_owner.json"
    if not (marker.exists() or marker.is_symlink()):
        return None
    database = resolve_session_owner(base)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT f.value,a.value FROM runtime f LEFT JOIN runtime a ON a.key=? WHERE f.key=?",
            (f"recovery:active:{slot_id}", f"recovery:file:{slot_id}"),
        ).fetchone()
    if row is None:
        return None
    record = decode_reference(slot_id, row[0], row[1])
    return record["version"], record["file"]
