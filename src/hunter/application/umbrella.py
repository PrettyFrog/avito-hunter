"""Umbrella query admission controller with recall verification and saturation guards (S7 / T-492).

Contracts:
- Experimental flag is OFF by default; umbrella admission strictly requires experimental flag.
- Exact local predicates: items must satisfy individual subscriber price ceilings and stopwords.
- Proof of recall required: all items captured by narrow query must be present in umbrella feed.
- Opaque, unparseable, or price-in-path filters strictly prohibit umbrella admission.
- Window saturation (total count or sponsored listings) rejects admission and triggers split only \
if spare capacity exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunter.domain.query_compatibility import compute_filter_fingerprint


def check_group_saturation_guard(
    item_count: int,
    sponsored_count: int = 0,
    window_limit: int = 50,
    saturation_threshold: float = 0.85,
) -> tuple[bool, str]:
    """Evaluates top-N saturation and ads displacement risk for query groups."""
    if window_limit <= 0:
        return True, "Invalid window limit"
    if item_count >= window_limit or (item_count / window_limit) >= saturation_threshold:
        return True, f"Top-N saturation risk: {item_count}/{window_limit} items"
    if sponsored_count > 0 and (sponsored_count / max(1, item_count)) >= 0.30:
        return True, f"Sponsored ads displacement risk: {sponsored_count}/{item_count} items"
    return False, "Coverage safe"


@dataclass(frozen=True, slots=True)
class GroupSplitPlan:
    group_id: str
    target_generation: int
    narrow_queries: list[str]
    required_capacity: int
    status: str
    degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SyntheticItem:
    id: str
    title: str
    price: int
    description: str = ""
    is_sponsored: bool = False


@dataclass(frozen=True, slots=True)
class UmbrellaCandidate:
    candidate_id: str
    search_url: str
    max_price: int
    stopwords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecallProof:
    expected_ids: tuple[str, ...]
    recalled_ids: tuple[str, ...]
    lost_ids: tuple[str, ...]
    recall_rate: float
    is_complete: bool


@dataclass(frozen=True, slots=True)
class UmbrellaDecision:
    admitted: bool
    reason: str
    recall: RecallProof | None = None
    is_saturated: bool = False
    can_split: bool = False
    eligible_bundle: list[SyntheticItem] = field(default_factory=list)


class UmbrellaAdmissionController:
    """Controls admission of narrow queries into broad umbrella queries with proof of recall."""

    def __init__(
        self,
        experimental_enabled: bool = False,
        window_size: int = 50,
        saturation_threshold: float = 0.85,
    ) -> None:
        self.experimental_enabled = experimental_enabled
        self.window_size = window_size
        self.saturation_threshold = saturation_threshold

    @staticmethod
    def check_opaque_filter(url: str, max_price: int) -> tuple[bool, str]:
        """Validates that filter parameters are transparent and mergeable."""
        _, mergeable, reason = compute_filter_fingerprint(url, max_price)
        return mergeable, reason

    @staticmethod
    def filter_locally(item: SyntheticItem, candidate: UmbrellaCandidate) -> bool:
        """Applies individual subscriber predicates locally on broad umbrella items."""
        if item.price > candidate.max_price:
            return False
        corpus = f"{item.title} {item.description}".lower()
        for sw in candidate.stopwords:
            sw_clean = sw.strip().lower()
            if sw_clean and sw_clean in corpus:
                return False
        return True

    def _check_saturation(
        self, wide_items: list[SyntheticItem], spare_capacity: int
    ) -> tuple[bool, bool, str]:
        sponsored = sum(1 for it in wide_items if it.is_sponsored)
        total = len(wide_items)
        saturated = (
            total >= self.window_size
            or (total / self.window_size) >= self.saturation_threshold
            or sponsored > 10
        )
        can_split = spare_capacity >= 1
        if saturated:
            reason = (
                "Saturated window: split recommended"
                if can_split
                else "Degraded: saturated window with no spare capacity to split"
            )
            return True, can_split, reason
        return False, can_split, "Capacity OK"

    def evaluate_admission(
        self,
        candidate: UmbrellaCandidate,
        narrow_items: list[SyntheticItem],
        wide_items: list[SyntheticItem],
        spare_capacity: int = 0,
    ) -> UmbrellaDecision:
        """Evaluates admission of candidate query into broad umbrella with proof of recall."""
        if not self.experimental_enabled:
            return UmbrellaDecision(
                admitted=False, reason="Umbrella experimental mode is disabled by default"
            )

        mergeable, reason = self.check_opaque_filter(candidate.search_url, candidate.max_price)
        if not mergeable:
            return UmbrellaDecision(
                admitted=False, reason=f"Opaque filter prohibits umbrella admission: {reason}"
            )

        is_sat, can_split, sat_reason = self._check_saturation(wide_items, spare_capacity)
        eligible = [
            it for it in wide_items if not it.is_sponsored and self.filter_locally(it, candidate)
        ]
        narrow_ids = tuple(it.id for it in narrow_items)
        wide_ids = set(it.id for it in eligible)
        lost = tuple(nid for nid in narrow_ids if nid not in wide_ids)
        recalled = tuple(nid for nid in narrow_ids if nid in wide_ids)
        rate = len(recalled) / len(narrow_ids) if narrow_ids else 1.0
        proof = RecallProof(narrow_ids, recalled, lost, rate, len(lost) == 0)

        if lost:
            return UmbrellaDecision(
                admitted=False,
                reason=f"Lost {len(lost)} tail item(s) in umbrella feed",
                recall=proof,
                is_saturated=is_sat,
                can_split=can_split,
            )
        if is_sat:
            return UmbrellaDecision(
                admitted=False,
                reason=sat_reason,
                recall=proof,
                is_saturated=True,
                can_split=can_split,
                eligible_bundle=eligible,
            )

        return UmbrellaDecision(
            admitted=True,
            reason="Recall proof verified",
            recall=proof,
            is_saturated=False,
            can_split=can_split,
            eligible_bundle=eligible,
        )


class GroupCoverageController:
    """Monitors completeness and safe splitting for price-sharing and umbrella groups."""

    def __init__(self, window_size: int = 50, saturation_threshold: float = 0.85) -> None:
        self.window_size = window_size
        self.saturation_threshold = saturation_threshold

    def can_admit_merge(self, item_count: int, sponsored_count: int = 0) -> tuple[bool, str]:
        sat, reason = check_group_saturation_guard(
            item_count, sponsored_count, self.window_size, self.saturation_threshold
        )
        if sat:
            return False, f"Stop new merges: {reason}"
        return True, "Merge admitted"

    def plan_split(
        self,
        group_id: str,
        current_generation: int,
        member_queries: list[str],
        spare_capacity: int,
    ) -> GroupSplitPlan:
        needed = max(0, len(member_queries) - 1)
        if spare_capacity >= needed:
            return GroupSplitPlan(
                group_id=group_id,
                target_generation=current_generation + 1,
                narrow_queries=list(member_queries),
                required_capacity=needed,
                status="ready",
            )
        return GroupSplitPlan(
            group_id=group_id,
            target_generation=current_generation + 1,
            narrow_queries=list(member_queries),
            required_capacity=needed,
            status="degraded",
            degraded_reason=f"Insufficient capacity to split group ({spare_capacity} < {needed})",
        )

    @staticmethod
    def handle_overflow(
        items: list[SyntheticItem], max_pages: int = 3, per_page: int = 50
    ) -> tuple[list[SyntheticItem], bool]:
        limit = max_pages * per_page
        if len(items) >= limit:
            return items[:limit], True
        return items, False
