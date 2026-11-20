"""Exact canonical query sharing and fanout planner (S7 / T-491).

Contracts:
- Level 1 query sharing: identical canonical URLs are merged into a single physical query schedule.
- Preserves all unknown/filter parameters: f, category, geo, radius, sort, district, etc.
- Different filters or different price ceilings never merge (strict equality).
- Separate fanout: results of a physical query are dispatched independently per subscription.
- 100 subscriptions over 40 exact queries yield exactly 40 schedules.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from hunter.domain.query_url import validate_and_canonicalize_query_url


def canonicalize_search_url(url: str, max_price: int) -> str:
    """Canonicalizes Avito search URL with deterministic parameter ordering."""
    base_canonical = validate_and_canonicalize_query_url(url, max_price)
    p = urlparse(base_canonical)
    qs = parse_qs(p.query, keep_blank_values=False)
    # Deterministic sorting of query keys and values
    sorted_qs = {k: sorted(v) for k, v in sorted(qs.items())}
    sorted_query = urlencode(sorted_qs, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, "", sorted_query, ""))


@dataclass(frozen=True, slots=True)
class Subscription:
    subscription_id: str
    user_id: int
    search_url: str
    max_price: int
    stopwords: tuple[str, ...] = ()
    canonical_url: str = ""

    def with_canonical_url(self, canonical_url: str) -> Subscription:
        return Subscription(
            subscription_id=self.subscription_id,
            user_id=self.user_id,
            search_url=self.search_url,
            max_price=self.max_price,
            stopwords=self.stopwords,
            canonical_url=canonical_url,
        )


@dataclass(frozen=True, slots=True)
class PhysicalQuery:
    query_id: str
    canonical_url: str
    max_price: int
    subscription_ids: tuple[str, ...]

    @property
    def subscribers_count(self) -> int:
        return len(self.subscription_ids)


@dataclass(frozen=True, slots=True)
class QueryPlan:
    physical_queries: list[PhysicalQuery]
    subscription_to_query: dict[str, str]
    total_subscriptions: int
    total_physical_queries: int
    saved_requests_per_poll: int


class CanonicalQueryPlanner:
    """Plans physical queries by exact canonical URL deduplication."""

    @staticmethod
    def _make_query_id(canonical_url: str) -> str:
        digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
        return f"q_{digest}"

    def plan_queries(self, subscriptions: list[Subscription]) -> QueryPlan:
        """Groups subscriptions by exact canonical URL; different filters never merge."""
        groups: dict[str, list[Subscription]] = {}
        for sub in subscriptions:
            canon = sub.canonical_url or canonicalize_search_url(sub.search_url, sub.max_price)
            resolved_sub = sub.with_canonical_url(canon)
            groups.setdefault(canon, []).append(resolved_sub)

        physical_queries: list[PhysicalQuery] = []
        sub_to_query: dict[str, str] = {}

        for canon_url, subs in groups.items():
            qid = self._make_query_id(canon_url)
            # All subscriptions in group share the exact same max_price by definition of canon_url
            max_p = subs[0].max_price
            sub_ids = tuple(s.subscription_id for s in subs)
            for sid in sub_ids:
                sub_to_query[sid] = qid
            physical_queries.append(
                PhysicalQuery(
                    query_id=qid,
                    canonical_url=canon_url,
                    max_price=max_p,
                    subscription_ids=sub_ids,
                )
            )

        total_subs = len(subscriptions)
        total_queries = len(physical_queries)
        saved = max(0, total_subs - total_queries)

        return QueryPlan(
            physical_queries=physical_queries,
            subscription_to_query=sub_to_query,
            total_subscriptions=total_subs,
            total_physical_queries=total_queries,
            saved_requests_per_poll=saved,
        )

    def filter_item_for_subscription(
        self,
        item_title: str,
        item_price: int,
        subscription: Subscription,
        item_description: str = "",
    ) -> bool:
        """Performs individual fanout filtering for a subscription."""
        if item_price > subscription.max_price:
            return False

        search_corpus = f"{item_title} {item_description}".lower()
        for sw in subscription.stopwords:
            sw_clean = sw.strip().lower()
            if sw_clean and sw_clean in search_corpus:
                return False

        return True

    def fanout_item(
        self,
        item: Any,
        subscriptions: list[Subscription],
    ) -> list[Subscription]:
        """Filters an incoming hit across all subscribed members independently."""
        title = getattr(item, "title", "") or ""
        price = getattr(item, "price", 0) or 0
        desc = getattr(item, "description", "") or ""

        eligible: list[Subscription] = []
        for s in subscriptions:
            if self.filter_item_for_subscription(title, price, s, desc):
                eligible.append(s)
        return eligible


class PersistedQueryPlanner:
    """Coordinates canonical planning with persisted query group repository."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.planner = CanonicalQueryPlanner()

    async def attach_subscription(
        self,
        subscription: Subscription,
        allow_compatible_price_sharing: bool = True,
        coverage_controller: Any = None,
        current_item_count: int = 0,
        sponsored_count: int = 0,
    ) -> tuple[Any, Any, bool]:
        if coverage_controller is not None:
            can_merge, _ = coverage_controller.can_admit_merge(current_item_count, sponsored_count)
            if not can_merge:
                allow_compatible_price_sharing = False

        canon = subscription.canonical_url or canonicalize_search_url(
            subscription.search_url, subscription.max_price
        )
        return await self.repo.attach(
            subscription_id=subscription.subscription_id,
            user_id=subscription.user_id,
            search_url=subscription.search_url,
            max_price=subscription.max_price,
            stopwords=subscription.stopwords,
            canonical_url=canon,
            allow_compatible_price_sharing=allow_compatible_price_sharing,
        )

    async def detach_subscription(
        self,
        group_id: str,
        subscription_id: str,
        user_id: int,
    ) -> bool:
        return await self.repo.detach(group_id, subscription_id, user_id)

    async def widen_group_bounds(
        self,
        group_id: str,
        new_ceiling: int,
        search_url: str,
        transition_capacity: int = 0,
    ) -> tuple[Any, bool, str]:
        new_canon = canonicalize_search_url(search_url, new_ceiling)
        return await self.repo.widen_ceiling(group_id, new_ceiling, new_canon, transition_capacity)

    async def shrink_group_bounds(
        self,
        group_id: str,
        search_url: str,
    ) -> tuple[Any, bool]:
        grp = await self.repo.get_group(group_id)
        if not grp:
            return None, False
        members = await self.repo.get_members(group_id)
        new_max = max((m.personal_max_price for m in members), default=grp.max_price)
        new_canon = canonicalize_search_url(search_url, new_max)
        return await self.repo.shrink_ceiling(group_id, new_canon)

    async def filter_poll_response(
        self,
        group_id: str,
        response_generation: int,
        items: list[Any],
    ) -> tuple[bool, dict[str, list[Any]], str]:
        grp = await self.repo.get_group(group_id)
        if not grp:
            return False, {}, "Group not found"
        if response_generation < grp.generation:
            return False, {}, f"Stale response rejected ({response_generation} < {grp.generation})"
        members = await self.repo.get_members(group_id)
        fanout_map: dict[str, list[Any]] = {}
        for m in members:
            sub = Subscription(m.subscription_id, m.user_id, "", m.personal_max_price, m.stopwords)
            fanout_map[m.subscription_id] = [
                it
                for it in items
                if self.planner.filter_item_for_subscription(
                    getattr(it, "title", ""),
                    getattr(it, "price", 0),
                    sub,
                    getattr(it, "description", ""),
                )
            ]
        return True, fanout_map, "Dispatched"
