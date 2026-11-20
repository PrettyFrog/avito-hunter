"""Task-owned preparation context; only activation may publish its candidate."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import json
import logging
import os


@dataclass
class PreparedPublication:
    slot_id: str
    version: int
    generation: int
    candidate: Path | None = None
    previous_reference: dict | None = None
    owner_task: object = field(default_factory=asyncio.current_task, repr=False)

    def matches(self, slot_id: str, version: int, generation: int) -> bool:
        return (self.owner_task is asyncio.current_task()
                and (self.slot_id, self.version, self.generation) == (slot_id, version, generation))


class PreparationScope:
    def __init__(self):
        self._current: ContextVar[PreparedPublication | None] = ContextVar("session_preparation", default=None)

    @property
    def current(self) -> PreparedPublication | None:
        return self._current.get()

    @contextmanager
    def reserve(self, slot_id: str, version: int, generation: int):
        current = self.current
        if current is not None and current.matches(slot_id, version, generation):
            raise RuntimeError("Duplicate profile preparation is forbidden")
        prepared = PreparedPublication(slot_id, version, generation)
        token = self._current.set(prepared)
        try:
            yield prepared
        finally:
            self._current.reset(token)


def project_legacy_profile(store, prepared: PreparedPublication) -> None:
    """Compatibility export after activation; committed references stay authoritative."""
    target = store._slot_path(prepared.slot_id)
    try:
        if target.exists():
            existing = json.loads(target.read_text())["metadata"]["version"]
            if existing > prepared.version:
                return
        store._write_atomic(target, prepared.candidate.read_text(), prepared.slot_id)
        fd = os.open(store.base_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        logging.getLogger(__name__).warning("Legacy projection failed; committed reference remains active")
