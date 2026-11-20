"""Session and client identity helpers for challenge recovery."""
from __future__ import annotations

from typing import Any
from enum import Enum


class RecoveryFailureKind(str, Enum):
    UNKNOWN = "unknown"
    NONE = "none"
    RECOVERY_FAILED = "recovery_failed"
    TRANSPORT_ERROR = "transport_error"
    UNSUPPORTED = "unsupported"
    INVALID_PROFILE = "invalid_profile"


def canonical_identity(value: str | None) -> str | None:
    if value in ("user_a", "a"):
        return "user_a"
    if value in ("user_b", "b", "__user_b"):
        return "user_b"
    return None


def peer_identity(value: str | None) -> str | None:
    canon = canonical_identity(value)
    if canon == "user_a":
        return "user_b"
    if canon == "user_b":
        return "user_a"
    return None


def classify_recovery_exception(exc: Exception) -> RecoveryFailureKind:
    import httpx
    if isinstance(exc, httpx.RequestError):
        return RecoveryFailureKind.TRANSPORT_ERROR
    try:
        from curl_cffi.requests.exceptions import RequestException
    except ImportError:
        return RecoveryFailureKind.RECOVERY_FAILED
    return RecoveryFailureKind.TRANSPORT_ERROR if isinstance(exc, RequestException) else RecoveryFailureKind.RECOVERY_FAILED

CLIENT_USER_A = "user_a"
CLIENT_USER_B = "user_b"


class CaseInsensitiveDict(dict):
    """Case-insensitive dictionary preserving original key case for iteration while allowing case-insensitive lookup."""

    def __init__(self, data: Any = None, **kwargs: Any) -> None:
        super().__init__()
        self._lower_map: dict[str, str] = {}
        if data:
            if hasattr(data, "items"):
                for k, v in data.items():
                    self[k] = v
            else:
                for k, v in data:
                    self[k] = v
        if kwargs:
            for k, v in kwargs.items():
                self[k] = v

    def __setitem__(self, key: str, value: Any) -> None:
        lk = str(key).lower()
        if lk in self._lower_map:
            old_k = self._lower_map[lk]
            if old_k != key:
                super().pop(old_k, None)
        self._lower_map[lk] = key
        super().__setitem__(key, value)

    def __getitem__(self, key: str) -> Any:
        lk = str(key).lower()
        if lk in self._lower_map:
            return super().__getitem__(self._lower_map[lk])
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        lk = str(key).lower()
        if lk in self._lower_map:
            return super().get(self._lower_map[lk], default)
        return default

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key.lower() in self._lower_map
        return False

    def update(self, *args: Any, **kwargs: Any) -> None:
        for k, v in dict(*args, **kwargs).items():
            self[k] = v

    def pop(self, key: str, default: Any = ...) -> Any:
        lk = str(key).lower()
        if lk in self._lower_map:
            real_k = self._lower_map.pop(lk)
            return super().pop(real_k)
        if default is not ...:
            return default
        raise KeyError(key)


def resolve_session_slot_id(slot_id: str, client_identity: str | None = None) -> str:
    """Returns physical session store key for the given slot and client identity."""
    return f"{slot_id}__user_b" if canonical_identity(client_identity) == CLIENT_USER_B else slot_id


def is_user_b_identity(client_identity: str | None) -> bool:
    return client_identity in (CLIENT_USER_B, "b", "__user_b")
