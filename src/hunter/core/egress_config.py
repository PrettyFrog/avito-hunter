"""Explicit public egress pacing configuration; no daily request quota."""
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EgressRegulatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    mode: Literal["off", "observe", "enforce"] = "off"
    interval_sec: float = Field(default=0.0, ge=0)
    night_interval_sec: float | None = Field(default=None, ge=0)
    admission_timeout_sec: float = Field(default=120.0, gt=0)
    max_cooldown_sec: float = Field(default=902.58, ge=0)
    max_active: int = Field(default=32, ge=1, le=1024)
    max_waiters: int = Field(default=128, ge=1, le=1024)
    groups: dict[str, str] = Field(default_factory=dict)

    @field_validator("interval_sec", "night_interval_sec", "admission_timeout_sec", "max_cooldown_sec")
    @classmethod
    def finite_interval(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError("Egress time intervals must be finite")
        return value

    @field_validator("groups")
    @classmethod
    def opaque_groups(cls, value):
        if any(not key or len(key) > 128 or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", group) is None
               for key, group in value.items()):
            raise ValueError("Invalid opaque egress group mapping")
        return dict(value)

    @model_validator(mode="after")
    def enforce_intervals(self):
        night = self.interval_sec if self.night_interval_sec is None else self.night_interval_sec
        if self.mode == "enforce":
            if min(self.interval_sec, night) <= 0:
                raise ValueError("Enforcement requires explicit positive request intervals")
            if self.admission_timeout_sec <= max(self.interval_sec, night):
                raise ValueError("Admission deadline must allow at least one request window")
        return self

    def group_for(self, slot_id: str, endpoint: str | None) -> str:
        configured = self.groups.get(slot_id)
        if endpoint in (None, "direct", "direct://"):
            if configured is not None and configured != "default":
                raise ValueError("Direct slots must share the default egress group")
            return "default"
        return configured or "default"

    def spacing_for(self, *, is_day: bool) -> float:
        if is_day or self.night_interval_sec is None:
            return self.interval_sec
        return self.night_interval_sec
