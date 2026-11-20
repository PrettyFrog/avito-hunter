"""Domain types for delivery events (S3 / T-456).

Only two event types delivered to users:
- NEW_LISTING: first appearance of an avito_id in search results after baseline.
- PRICE_CHANGED: any price change in either direction (even 1 ruble).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    NEW_LISTING = "NEW_LISTING"
    PRICE_CHANGED = "PRICE_CHANGED"


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    event_type: EventType
    avito_id: str
    query_id: str
    created_at: str
    new_price: int | None
    old_price: int | None = None
    revision: int = 1
    title: str = ""
    url: str = ""
    location: str | None = None
    image_url: str | None = None
    provenance: dict[str, Any] | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError(f"Unsupported Event version: {self.version}")
        if not self.event_id or not isinstance(self.event_id, str):
            raise ValueError("event_id must be a non-empty string")
        if not self.avito_id or not isinstance(self.avito_id, str):
            raise ValueError("avito_id must be a non-empty string")
        if not self.query_id or not isinstance(self.query_id, str):
            raise ValueError("query_id must be a non-empty string")
        if not self.created_at or not isinstance(self.created_at, str):
            raise ValueError("created_at must be an ISO 8601 UTC string")
        if self.revision < 1:
            raise ValueError("revision must be >= 1")

        if self.event_type == EventType.PRICE_CHANGED:
            if self.old_price is None or self.new_price is None:
                raise ValueError("PRICE_CHANGED event requires both old_price and new_price")
            if self.old_price == self.new_price:
                raise ValueError("PRICE_CHANGED event requires old_price != new_price")
        elif self.event_type == EventType.NEW_LISTING:
            if self.old_price is not None:
                raise ValueError("NEW_LISTING event must have old_price=None")
        else:
            raise ValueError(
                f"Unsupported event type: {self.event_type}. "
                "Only NEW_LISTING and PRICE_CHANGED allowed."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "avito_id": self.avito_id,
            "query_id": self.query_id,
            "created_at": self.created_at,
            "new_price": self.new_price,
            "old_price": self.old_price,
            "revision": self.revision,
            "title": self.title,
            "url": self.url,
            "location": self.location,
            "image_url": self.image_url,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        ver = data.get("version", 1)
        if ver != 1:
            raise ValueError(f"Unsupported Event version: {ver}")
        raw_type = data["event_type"]
        if raw_type not in (EventType.NEW_LISTING, EventType.PRICE_CHANGED):
            raise ValueError(f"Unsupported event type: {raw_type}")
        return cls(
            version=ver,
            event_id=str(data["event_id"]),
            event_type=EventType(raw_type),
            avito_id=str(data["avito_id"]),
            query_id=str(data["query_id"]),
            created_at=str(data["created_at"]),
            new_price=int(data["new_price"]) if data.get("new_price") is not None else None,
            old_price=int(data["old_price"]) if data.get("old_price") is not None else None,
            revision=int(data.get("revision", 1)),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            location=data.get("location"),
            image_url=data.get("image_url"),
            provenance=data.get("provenance"),
        )
