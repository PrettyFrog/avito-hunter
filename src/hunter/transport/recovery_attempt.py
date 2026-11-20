"""Attempt-scoped commit receipt for challenge recovery."""
from __future__ import annotations
from dataclasses import dataclass, field
import uuid

@dataclass(slots=True)
class RecoveryAttemptReceipt:
    slot_id: str
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    committed: bool = False
    new_version: int | None = None
    def mark_committed(self, new_version: int) -> None:
        self.committed, self.new_version = True, new_version
    @property
    def is_committed(self) -> bool:
        return self.committed and self.new_version is not None
