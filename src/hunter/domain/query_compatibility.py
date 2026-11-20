"""Query compatibility analysis and multi-ceiling price grouping (S7 / T-502).

Contracts:
- Level 2 grouping: same underlying filters with differing price ceilings.
- Group ceiling = max(user_ceilings); NO synthetic +20% headroom.
- Locally strict ceilings: items above individual ceiling are rejected locally on fanout.
- Personal stopwords: evaluated locally per subscription.
- Strict isolation: city, radius, sort, delivery flags, or unknown f parameters never merge.
- Price in path or unproven equivalence strictly prohibits merging.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from hunter.application.query_planner import canonicalize_search_url


def _extract_f_non_price(f_val: str) -> tuple[dict[str, Any] | None, int | None]:
    """Extracts non-price JSON attributes from base64 'f' filter param."""
    try:
        padded = f_val + "=" * ((4 - len(f_val) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded)
        idx = raw.find(b'{"')
        if idx == -1:
            return None, None
        data = json.loads(raw[idx:].decode("utf-8"))
        if not isinstance(data, dict):
            return None, None
        to_val = data.get("to")
        non_price = {k: v for k, v in data.items() if k not in ("to", "from")}
        return non_price, int(to_val) if to_val is not None else None
    except Exception:
        return None, None


def compute_filter_fingerprint(url: str, max_price: int) -> tuple[str, bool, str]:
    """Computes a deterministic fingerprint representing all non-price filters.

    Returns:
        (fingerprint, is_mergeable, reason)
    """
    if not isinstance(max_price, int) or max_price <= 0:
        return ("", False, "max_price must be a positive integer")

    p = urlparse(str(url).strip())
    if p.scheme.lower() != "https":
        return ("", False, "Scheme must be https")

    host = (p.hostname or "").lower()
    if "avito.ru" not in host:
        return ("", False, "Host must be avito.ru")

    path = p.path.lower().rstrip("/")
    if re.search(r"/(do|ot|cena)-\d+", path):
        return ("", False, "Price in path prohibits grouping without route verification")

    qs = parse_qs(p.query, keep_blank_values=False)

    f_repr = ""
    if "f" in qs and qs["f"]:
        non_price_f, _ = _extract_f_non_price(qs["f"][0])
        if non_price_f is None:
            return ("", False, "Unsupported or unparseable 'f' parameter prohibits grouping")
        f_repr = json.dumps(non_price_f, sort_keys=True)

    # Strip price and tracking parameters to isolate underlying search filters
    strip_keys = {"pmax", "pmin", "f", "p", "page", "context", "from", "presentationType"}
    clean_qs: dict[str, list[str]] = {}
    for k, v in qs.items():
        if k not in strip_keys and not k.startswith("utm_"):
            clean_qs[k] = sorted(v)

    if "s" not in clean_qs:
        clean_qs["s"] = ["104"]

    sorted_query = urlencode(sorted(clean_qs.items()), doseq=True)
    fp = f"host={host}|path={path}|query={sorted_query}|f_non_price={f_repr}"
    return (fp, True, "OK")


@dataclass(frozen=True, slots=True)
class CompatibleSubscription:
    subscription_id: str
    user_id: int
    search_url: str
    max_price: int
    stopwords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PriceGroup:
    group_id: str
    base_url: str
    group_ceiling: int
    filter_fingerprint: str
    members: tuple[CompatibleSubscription, ...]

    @property
    def member_count(self) -> int:
        return len(self.members)


class QueryCompatibility:
    """Evaluates filter compatibility and groups searches with differing price ceilings."""

    def are_compatible(self, url1: str, price1: int, url2: str, price2: int) -> bool:
        fp1, ok1, _ = compute_filter_fingerprint(url1, price1)
        fp2, ok2, _ = compute_filter_fingerprint(url2, price2)
        if not ok1 or not ok2:
            return False
        return fp1 == fp2

    def group_subscriptions(
        self, subscriptions: list[CompatibleSubscription]
    ) -> list[PriceGroup]:
        """Groups subscriptions by non-price filter equality, setting group ceiling to max."""
        groups: dict[str, list[CompatibleSubscription]] = {}
        unmergeable: list[CompatibleSubscription] = []

        for sub in subscriptions:
            fp, ok, _ = compute_filter_fingerprint(sub.search_url, sub.max_price)
            if not ok:
                unmergeable.append(sub)
            else:
                groups.setdefault(fp, []).append(sub)

        result: list[PriceGroup] = []
        for fp, group_subs in groups.items():
            max_ceiling = max(s.max_price for s in group_subs)
            base_url = canonicalize_search_url(group_subs[0].search_url, max_price=max_ceiling)
            gid = hashlib.sha256(f"{fp}|{max_ceiling}".encode("utf-8")).hexdigest()[:16]
            result.append(
                PriceGroup(
                    group_id=f"grp_{gid}",
                    base_url=base_url,
                    group_ceiling=max_ceiling,
                    filter_fingerprint=fp,
                    members=tuple(group_subs),
                )
            )

        for un_sub in unmergeable:
            gid = hashlib.sha256(f"{un_sub.subscription_id}".encode("utf-8")).hexdigest()[:16]
            result.append(
                PriceGroup(
                    group_id=f"grp_solo_{gid}",
                    base_url=un_sub.search_url,
                    group_ceiling=un_sub.max_price,
                    filter_fingerprint="",
                    members=(un_sub,),
                )
            )

        return result

    def filter_for_subscription(
        self,
        item_price: int,
        item_title: str,
        subscription: CompatibleSubscription,
        item_description: str = "",
    ) -> bool:
        """Strictly enforces subscription's personal price ceiling and manual stopwords."""
        if item_price > subscription.max_price:
            return False

        corpus = f"{item_title} {item_description}".lower()
        for sw in subscription.stopwords:
            sw_clean = sw.strip().lower()
            if sw_clean and sw_clean in corpus:
                return False

        return True
