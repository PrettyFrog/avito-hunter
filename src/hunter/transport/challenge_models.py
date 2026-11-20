"""Stable public challenge contracts and non-destructive failure provenance."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hunter.transport.recovery_identity import RecoveryFailureKind


class ChallengeStatus(str, Enum):
    SOLVED = "solved"
    OPERATOR_REQUIRED = "operator_required"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    QUEUE_FULL = "queue_full"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ChallengeOutcome:
    slot_id: str
    status: ChallengeStatus
    solved: bool
    new_session_version: int | None
    quarantined: bool
    reason: str = ""
    client_identity: str = ""
    failure_kind: RecoveryFailureKind = RecoveryFailureKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class ChallengeRequest:
    slot_id: str
    challenge_type: str = "pow"
    html: str = ""
    timeout_sec: float | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: Any = None
    challenge_token: str | None = None
    client_identity: str = ""
    http_client: Any = None
    endpoint: str | None = None
    proxy_credentials: Any = None
    expected_generation: int | None = None
    assignment_generation: int | None = None
    query_id: str | None = None
    allow_quarantine: bool = True

