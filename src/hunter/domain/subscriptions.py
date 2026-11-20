"""Domain types for subscriptions and user filters (S3 / T-456).

Strict ceiling (zero headroom), manual stop words only, tenant isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Subscription:
    id: str
    user_id: int
    query_id: str
    search_url: str
    max_price: int
    stop_words: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    enabled: bool = True
    ai_enrichment_enabled: bool = False
    created_at: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError(f"Unsupported Subscription version: {self.version}")
        if not self.id or not isinstance(self.id, str):
            raise ValueError("id must be a non-empty string")
        if not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if not self.query_id or not isinstance(self.query_id, str):
            raise ValueError("query_id must be a non-empty string")
        if not self.search_url or not isinstance(self.search_url, str):
            raise ValueError("search_url must be a non-empty string")
        if not isinstance(self.max_price, int) or self.max_price < 0:
            raise ValueError("max_price must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "user_id": self.user_id,
            "query_id": self.query_id,
            "search_url": self.search_url,
            "max_price": self.max_price,
            "stop_words": list(self.stop_words),
            "keywords": list(self.keywords),
            "enabled": self.enabled,
            "ai_enrichment_enabled": self.ai_enrichment_enabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Subscription:
        ver = data.get("version", 1)
        if ver != 1:
            raise ValueError(f"Unsupported Subscription version: {ver}")
        raw_words = data.get("stop_words", ())
        stop_words = tuple(str(w).strip().lower() for w in raw_words if str(w).strip())
        raw_kws = data.get("keywords", ())
        keywords = tuple(str(w).strip().lower() for w in raw_kws if str(w).strip())
        return cls(
            version=ver,
            id=str(data["id"]),
            user_id=int(data["user_id"]),
            query_id=str(data["query_id"]),
            search_url=str(data["search_url"]),
            max_price=int(data["max_price"]),
            stop_words=stop_words,
            keywords=keywords,
            enabled=bool(data.get("enabled", True)),
            ai_enrichment_enabled=bool(data.get("ai_enrichment_enabled", False)),
            created_at=str(data.get("created_at", "")),
        )
