"""Inspect recovery receipts against durable active session versions."""
import json
from pathlib import Path
from typing import Any

from hunter.domain.slots import validate_slot_id

PREFIX = "recovery:active:"


def active_versions(rows: Any) -> dict[str, int]:
    return {key[len(PREFIX):]: int(value) for key, value in rows}


def receipt_is_obsolete(path: Path, base: Path, versions: dict[str, int]) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    sid = data.get("session_slot_id") or data.get("slot_id")
    if not isinstance(version, int) or not isinstance(sid, str):
        return False
    validate_slot_id(sid)
    if versions.get(sid, 0) >= version:
        return True
    target = base / f"slot_{sid}_session.json"
    if target.is_file():
        metadata = json.loads(target.read_text(encoding="utf-8")).get("metadata", {})
        return int(metadata.get("version", 0)) > version
    return False
