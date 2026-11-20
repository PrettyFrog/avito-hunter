"""Opaque correlation labels; egress identity is supplied explicitly by the owner."""
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any


@dataclass(frozen=True, slots=True)
class AttemptLabels:
    slot_id: str
    identity: str
    egress_group: str | None = None

    def __post_init__(self) -> None:
        if self.identity not in {"user_a", "user_b", "unknown"}:
            raise ValueError("Unknown session identity")

    def emitter(self, sink: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
        slot = sha256(self.slot_id.encode()).hexdigest()[:24]
        group = sha256(self.egress_group.encode()).hexdigest()[:24] if self.egress_group is not None else "unknown"
        def emit(event: dict[str, Any]) -> None:
            sink({**event, "slot": slot, "identity": self.identity, "egress_group": group})
        return emit
