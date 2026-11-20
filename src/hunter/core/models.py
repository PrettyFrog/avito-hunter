from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from hunter.domain.events import Event as Event, EventType as EventType
from hunter.domain.observations import (
    ListingId as ListingId,
    ObservationBatch as ObservationBatch,
    ObservedListing as ObservedListing,
    ParseOutcome as ParseOutcome,
    PriceState as PriceState,
)
from hunter.domain.subscriptions import Subscription as Subscription


class NotifyMode(StrEnum):
    new = "new"
    deals = "deals"
    silent = "silent"
    shadow = "shadow"


class SellerFilter(StrEnum):
    private = "private"
    any = "any"


class WatchItem(BaseModel):
    id: str
    category: str
    models: list[str]
    max_price: int
    seller: SellerFilter = SellerFilter.private
    poll_minutes: int | None = None
    poll_seconds: int | None = None
    search_urls: list[str]
    enabled: bool = True
    is_gem_feed: bool = False
    priority: str | None = None
    notify_mode: str | None = None
    price_headroom_pct: float = 0.0
    owner_user_id: int | None = None

    @property
    def effective_poll_seconds(self) -> int:
        if self.poll_seconds:
            return max(1, self.poll_seconds)
        if self.poll_minutes:
            return max(1, self.poll_minutes * 60)
        from hunter.core.config import get_settings
        settings = get_settings()
        priority_intervals = getattr(settings, "priority_intervals", None) or {"high": 15, "normal": 30, "low": 60}
        prio_min = priority_intervals.get(self.priority or "normal", 30)
        return max(1, prio_min * 60)



class SearchHit(BaseModel):
    avito_id: str
    url: str
    title: str | None = None
    price: int | None = None
    old_price: int | None = None
    discount_pct: float | None = None
    price_suspect: bool = False
    price_state: str = "missing"
    city: str | None = None
    seller_type: str | None = None
    snippet: str | None = None
    published_at: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Card(BaseModel):
    avito_id: str
    url: str
    title: str | None = None
    price: int | None = None
    old_price: int | None = None
    discount_pct: float | None = None
    price_suspect: bool = False
    description: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    city: str | None = None
    delivery: bool | None = None
    seller_id: str | None = None
    seller_type: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    published: str | None = None
    published_at: str | None = None


class MatchResult(BaseModel):
    model_id: str | None = None
    confidence: float = 0.0
    matched_alias: str | None = None
    source: str = "none"
    category: str | None = None


class Extraction(BaseModel):
    model_id: str | None = None
    specs: dict[str, Any] = Field(default_factory=dict)
    condition: str = "used"
    defects: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    source: str = "rules"
    mismatch: bool = False
    low_info: bool = False
    body_only: bool = False
    red_flags: list[str] = Field(default_factory=list)



class Round1Decision(BaseModel):
    pass_stub: bool
    skip_reason: str | None = None
    reasons: list[str] = Field(default_factory=list)
    extraction: Extraction | None = None
    discount: float | None = None
    ev: int | None = None
    verdict: str = "skip"


class NotificationEvent(BaseModel):
    avito_id: str
    url: str
    title: str
    price: int | None
    max_price: int
    watchlist_id: str
    category: str
    model_id: str | None
    verdict: str
    reasons: list[str]
    discount: float | None = None
    ev: int | None = None
    image_url: str | None = None
    seller_message: str | None = None
    checklist: list[str] = Field(default_factory=list)
    price_reduced_from_above_max: tuple[int, int] | None = None
    recipient_user_id: int | None = None
    recipient_chat_id: int | None = None
    owner_user_id: int | None = None
    city: str | None = None
    age_text: str | None = None
    published_at: str | None = None
    is_gem_feed: bool = False
    photo_key: str | None = None
    specs: dict[str, Any] = Field(default_factory=dict)


class User(BaseModel):
    id: int
    telegram_user_id: int
    chat_id: int
    username: str | None = None
    display_name: str | None = None
    role: str = "user"  # "owner" | "user"
    status: str = "active"  # "pending" | "active" | "paused" | "inactive" | "blocked"
    tier: str = "friend"  # "owner" | "family" | "friend"
    max_products: int = 3
    min_interval_minutes: int = 15
    max_alerts_per_hour: int = 10
    quiet_start: int | None = None
    quiet_end: int | None = None
    timezone: str = "Europe/Moscow"
    invited_by: int | None = None
    created_at: str
    last_seen_at: str | None = None
    last_error: str | None = None

    @property
    def telegram_id(self) -> int:
        return self.telegram_user_id

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def is_active(self) -> bool:
        return self.status == "active"



class FetchResult(BaseModel):
    url: str
    status: int
    html: str
    json_blobs: list[Any] = Field(default_factory=list)
    final_url: str | None = None
    captcha: bool = False
    blocked: bool = False
    latency_ms: int = 0
    timeout: bool = False
    profile_id: str | None = None


class GroupPollResponse(BaseModel):
    group_id: str
    generation: int
    items: list[Any] = Field(default_factory=list)
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def is_stale(self, active_generation: int) -> bool:
        return self.generation < active_generation


class QueryGroupBounds(BaseModel):
    group_id: str
    current_ceiling: int
    generation: int
    version: int
    is_transitioning: bool = False
    transition_capacity: int = 0


class PriceCoverageContractDTO(BaseModel):
    avito_id: str
    known_price: int | None = None
    price_state: str = "active"
    coverage_mode: str = "serp_only"
    is_observed_in_serp: bool = True
    revision: int = 1
    last_seen_at: str = Field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat())




def now_utc() -> datetime:
    return datetime.now(UTC)


def utcnow() -> str:
    return now_utc().replace(microsecond=0).isoformat()

