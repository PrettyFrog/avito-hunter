"""Compatibility shim for CutoverMode and dummy CutoverManager (Pure V2)."""
from __future__ import annotations

from enum import Enum
from typing import Any


class CutoverMode(str, Enum):
    LEGACY = "legacy"
    DUAL_EVAL = "dual_eval"
    DRAINING = "draining"
    V2 = "v2"


class TenantCutoverState:
    def __init__(self, mode: CutoverMode = CutoverMode.V2) -> None:
        self.mode = mode
        self.publisher_generation = 1
        self.generation = 1
        self.cutoff_sequence = 0


class CutoverManager:
    """No-op stub: all tenants are pure V2."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_tenant_state(self, user_id: int) -> TenantCutoverState:
        return TenantCutoverState(CutoverMode.V2)

    async def set_tenant_mode(
        self, user_id: int, mode: Any, *args: Any, **kwargs: Any
    ) -> TenantCutoverState:
        return TenantCutoverState(CutoverMode.V2)

    async def ensure_v2_if_absent(self, user_id: int) -> TenantCutoverState:
        return TenantCutoverState(CutoverMode.V2)

    async def is_v2_publisher(self, user_id: int) -> bool:
        return True

    async def is_legacy_publisher(self, user_id: int) -> bool:
        return False

    async def is_dual_eval(self, user_id: int) -> bool:
        return False

    async def recover_ready_drains(self) -> None:
        pass
