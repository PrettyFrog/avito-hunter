"""Domain types for poll observations and price states (S3 / T-456)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PriceState(StrEnum):
    ACTIVE = "active"
    DISCOUNT_TRAP = "discount_trap"
    FREE_OR_NEGOTIATED = "free_or_negotiated"
    SUSPECT = "suspect"
    UNKNOWN = "unknown"


class PriceCoverageMode(StrEnum):
    SERP_ONLY = "serp_only"
    ACTIVE_WATCH = "active_watch"
    PAUSED = "paused"
    RETIRED = "retired"


class ParseOutcome(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    CUTOFF = "cutoff"
    CAPTCHA = "captcha"
    BLOCKED_403 = "blocked_403"
    RATE_LIMIT_429 = "rate_limit_429"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"


type ListingId = str


@dataclass(frozen=True, slots=True)
class PriceObservationRecord:
    avito_id: str
    known_price: int | None
    price_state: PriceState = PriceState.ACTIVE
    coverage_mode: PriceCoverageMode = PriceCoverageMode.SERP_ONLY
    is_observed_in_serp: bool = True
    last_seen_at: str = ""
    revision: int = 1

    def apply_serp_observation(
        self,
        new_price: int | None,
        new_price_state: PriceState = PriceState.ACTIVE,
        observed_at: str = "",
    ) -> tuple[PriceObservationRecord, str | None]:
        """Applies newly observed SERP hit; emits PRICE_CHANGED only when price actually differs."""
        if new_price is None or new_price == self.known_price:
            return PriceObservationRecord(
                avito_id=self.avito_id,
                known_price=self.known_price,
                price_state=new_price_state,
                coverage_mode=self.coverage_mode,
                is_observed_in_serp=True,
                last_seen_at=observed_at or self.last_seen_at,
                revision=self.revision,
            ), None

        return PriceObservationRecord(
            avito_id=self.avito_id,
            known_price=new_price,
            price_state=new_price_state,
            coverage_mode=self.coverage_mode,
            is_observed_in_serp=True,
            last_seen_at=observed_at or self.last_seen_at,
            revision=self.revision + 1,
        ), "PRICE_CHANGED"

    def apply_absence_from_serp(self, observed_at: str = "") -> PriceObservationRecord:
        (
            'Records absence from current SERP batch without corrupting known price '
            'or emitting fake events.'
        )
        return PriceObservationRecord(
            avito_id=self.avito_id,
            known_price=self.known_price,
            price_state=self.price_state,
            coverage_mode=self.coverage_mode,
            is_observed_in_serp=False,
            last_seen_at=self.last_seen_at,
            revision=self.revision,
        )

    def with_coverage_mode(self, mode: PriceCoverageMode) -> PriceObservationRecord:
        return PriceObservationRecord(
            avito_id=self.avito_id,
            known_price=self.known_price,
            price_state=self.price_state,
            coverage_mode=mode,
            is_observed_in_serp=self.is_observed_in_serp,
            last_seen_at=self.last_seen_at,
            revision=self.revision,
        )



@dataclass(frozen=True, slots=True)
class ObservedListing:
    avito_id: str
    title: str
    price: int | None
    price_state: PriceState
    url: str
    location: str | None = None
    image_url: str | None = None
    published_at: str | None = None
    raw_snippet: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.avito_id or not isinstance(self.avito_id, str):
            raise ValueError("avito_id must be a non-empty string")
        if not self.url or not isinstance(self.url, str):
            raise ValueError("url must be a non-empty string")
        if self.price is not None and self.price < 0:
            raise ValueError("price cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "avito_id": self.avito_id,
            "title": self.title,
            "price": self.price,
            "price_state": str(self.price_state),
            "url": self.url,
            "location": self.location,
            "image_url": self.image_url,
            "published_at": self.published_at,
            "raw_snippet": self.raw_snippet,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ObservedListing:
        return cls(
            avito_id=str(data["avito_id"]),
            title=str(data.get("title", "")),
            price=int(data["price"]) if data.get("price") is not None else None,
            price_state=PriceState(data.get("price_state", PriceState.ACTIVE)),
            url=str(data["url"]),
            location=data.get("location"),
            image_url=data.get("image_url"),
            published_at=data.get("published_at"),
            raw_snippet=data.get("raw_snippet"),
        )


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    slot_id: str
    query_id: str
    observed_at: str
    outcome: ParseOutcome
    listings: tuple[ObservedListing, ...] = ()
    latency_ms: int = 0
    http_status: int = 200
    error_message: str | None = None
    version: int = 1
    assignment_generation: int | None = None
    tenant_generations: dict[int, int] | None = None

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError(f"Unsupported ObservationBatch version: {self.version}")
        if not self.slot_id or not isinstance(self.slot_id, str):
            raise ValueError("slot_id must be a non-empty string")
        if not self.query_id or not isinstance(self.query_id, str):
            raise ValueError("query_id must be a non-empty string")
        if not self.observed_at or not isinstance(self.observed_at, str):
            raise ValueError("observed_at must be an ISO 8601 UTC string")
        if self.assignment_generation is not None and self.assignment_generation < 1:
            raise ValueError("assignment_generation must be positive")
        if self.tenant_generations is not None and any(
            int(user_id) <= 0 or int(generation) < 0
            for user_id, generation in self.tenant_generations.items()
        ):
            raise ValueError(
                "tenant generation snapshots require positive users "
                "and non-negative generations"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "slot_id": self.slot_id,
            "query_id": self.query_id,
            "observed_at": self.observed_at,
            "outcome": str(self.outcome),
            "listings": [l.to_dict() for l in self.listings],
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
            "error_message": self.error_message,
            "assignment_generation": self.assignment_generation,
            "tenant_generations": self.tenant_generations,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ObservationBatch:
        ver = data.get("version", 1)
        if ver != 1:
            raise ValueError(f"Unsupported ObservationBatch version: {ver}")
        raw_listings = data.get("listings", [])
        listings = tuple(ObservedListing.from_dict(item) for item in raw_listings)
        return cls(
            version=ver,
            slot_id=str(data["slot_id"]),
            query_id=str(data["query_id"]),
            observed_at=str(data["observed_at"]),
            outcome=ParseOutcome(data["outcome"]),
            listings=listings,
            latency_ms=int(data.get("latency_ms", 0)),
            http_status=int(data.get("http_status", 200)),
            error_message=data.get("error_message"),
            assignment_generation=(
                int(data["assignment_generation"])
                if data.get("assignment_generation") is not None else None
            ),
            tenant_generations=(
                {int(uid): int(generation) for uid, generation in data[
                    'tenant_'
                    'generations'].items()}
                if data.get("tenant_generations") is not None else None
            ),
        )
