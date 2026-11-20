from __future__ import annotations

import re
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator

from hunter.core.models import SellerFilter, WatchItem


class ProductConflict(BaseModel):
    """Declarative sibling / older-generation / clickbait conflict rule.

    `pattern` is a regex evaluated against the *normalized* title. When it matches and
    none of the `unless` substrings are present in that title, the lot is rejected.
    `label` overrides the reported reason (defaults to the matched text).

    Keeping these rules in the product profile means adding a new product never requires
    editing the matching engine.
    """

    pattern: str
    unless: list[str] = Field(default_factory=list)
    label: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_bare_pattern(cls, data: Any) -> Any:
        # Allow the shorthand `conflicts: ["<regex>", ...]` form in YAML.
        if isinstance(data, str):
            return {"pattern": data}
        return data


class ProductMatch(BaseModel):
    policy: Literal["strict", "loose"] = "strict"
    aliases: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)
    loose_words: list[str] = Field(default_factory=list)
    include_defective: bool = False
    accessory_words: list[str] = Field(default_factory=list)
    conflicts: list[ProductConflict] = Field(default_factory=list)

    @field_validator("must_have", "aliases", "reject", "loose_words", "accessory_words", mode="before")
    @classmethod
    def parse_str_list(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(item) for item in v]
        return v


class ProductPhotos(BaseModel):
    refs: list[str] = Field(default_factory=list)
    negative_refs: list[str] = Field(default_factory=list)
    signature: dict[str, Any] | None = None
    photo_check: Literal["off", "warn", "strict"] = "off"
    check: Literal["off", "shadow", "describe", "compare", "both"] = "off"
    hard_skip_enabled: bool | None = None
    qualified_hard_skip: bool = False
    refs_verified: bool = False
    excluded_lots: list[str] = Field(default_factory=list)
    reference_product: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_and_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # 1. Migrate legacy hard_skip_enabled
            if "hard_skip_enabled" in data and data.get("hard_skip_enabled") is not None and "photo_check" not in data:
                old_val = data["hard_skip_enabled"]
                data["photo_check"] = "strict" if old_val else "warn"
                logger.info("Migrated legacy hard_skip_enabled={} to photo_check='{}'", old_val, data["photo_check"])
            # 2. Default photo_check based on presence of reference images
            elif "photo_check" not in data or data.get("photo_check") is None:
                refs = data.get("refs") or []
                data["photo_check"] = "strict" if len(refs) > 0 else "off"

            if data.get("check") is False or data.get("check") == "off":
                data["check"] = "off"
        return data


class ProductCoverage(BaseModel):
    backfill: bool = False
    deep_sweep: Literal["auto", "off"] = "off"
    max_pages: int = 20


class ProductLimits(BaseModel):
    max_cards_per_day: int | None = None
    max_photo_checks_per_day: int | None = 20


def populate_name_validator(v: Any, info: Any) -> str:
    """Validator logic for ProductProfile.name."""
    if v:
        return str(v)
    data = info.data if hasattr(info, "data") else {}
    return str(data.get("title") or data.get("id") or "")


def validate_id_validator(v: str) -> str:
    """Validator logic for ProductProfile.id."""
    if not re.match(r"^[a-zA-Z0-9_]+$", v):
        raise ValueError(f"id must match '^[a-zA-Z0-9_]+$', got '{v}'")
    return v


def normalize_search_url_validator(v: str) -> str:
    """Validator logic for ProductProfile.search_url."""
    cleaned = v.strip()
    if cleaned:
        from urllib.parse import urlparse

        parsed = urlparse(cleaned)
        netloc = (parsed.netloc or "").lower()
        allowed = ("www.avito.ru", "m.avito.ru", "avito.ru", "test", "localhost", "example.com")
        if netloc and not (netloc in allowed or netloc.endswith(".avito.ru")):
            raise ValueError(f"search_url domain must be avito.ru or m.avito.ru, got '{netloc}'")
    return cleaned


def normalize_profile_post_init(profile: Any) -> None:
    """Run search_url auto-normalization after model initialization."""
    try:
        from hunter.normalize.avito_url import normalize_avito_url

        if profile.search_url and profile.max_price > 0:
            norm_url, err = normalize_avito_url(
                profile.search_url,
                max_price=profile.max_price,
                headroom_pct=profile.headroom_pct,
                min_price=profile.min_price,
            )
            if norm_url:
                profile.search_url = norm_url
    except Exception as exc:
        logger.debug("search_url auto-normalization failed: {}", exc)


def infer_watch_category_for_profile(
    profile_id: str,
    name: str,
    aliases: list[str],
    loose_words: list[str],
    preset: str | None = None,
) -> str:
    """Infer category for watch item from profile tokens and presets."""
    from hunter.normalize.text import normalize_text

    blob = normalize_text(
        " ".join([profile_id, name] + list(aliases) + list(loose_words))
    )
    headphone_markers = (
        "наушники",
        "наушник",
        "headphones",
        "headphone",
        "earbuds",
        "wh 1000",
        "wh1000",
        "wf 1000",
        "wf1000",
        "quietcomfort",
        "px7",
        "px8",
        "melomania",
        "airpods",
        "momentum",
    )
    if any(m in blob for m in headphone_markers):
        return "headphones"
    laptop_markers = ("ноутбук", "thinkpad", "macbook", "laptop", "ultrabook", "ideapad", "legion")
    if any(m in blob for m in laptop_markers):
        return "laptops"
    return preset or "generic"


def build_watch_models(profile_id: str, policy: str, aliases: list[str]) -> list[str]:
    """Build list of models for pipeline WatchItem from profile policy and aliases."""
    if policy == "loose":
        models = list(aliases) if aliases else [profile_id]
        if not profile_id.startswith("gem_") and profile_id not in models:
            models = [profile_id] + models
        return models
    return [profile_id]


def resolve_poll_timing(
    poll_seconds: int | None,
    poll_minutes: int | None,
    priority: str,
) -> tuple[int, int]:
    """Resolve (poll_minutes, poll_seconds) tuple from profile overrides and priority."""
    if poll_seconds:
        p_sec = max(1, poll_seconds)
        p_min = max(1, p_sec // 60)
        return p_min, p_sec
    if poll_minutes:
        p_min = max(1, poll_minutes)
        p_sec = p_min * 60
        return p_min, p_sec
    if priority == "high":
        return 15, 15 * 60
    if priority == "low":
        return 60, 60 * 60
    return 30, 30 * 60


def resolve_notify_mode(mode: str) -> str:
    """Map ProductProfile.mode to WatchItem.notify_mode."""
    if mode == "shadow":
        return "shadow"
    if mode == "off":
        return "silent"
    return "new"


def resolve_seller_filter(seller: str | None, search_url: str | None) -> SellerFilter:
    """Resolve SellerFilter enum from profile.seller or search_url parameters."""
    if seller == "private":
        return SellerFilter.private
    if seller == "any":
        return SellerFilter.any
    if search_url and ("user=1" in search_url or "user=private" in search_url):
        return SellerFilter.private
    return SellerFilter.any


def build_watch_item_from_profile(profile: Any) -> WatchItem:
    """Map universal ProductProfile to pipeline WatchItem instance."""
    models = build_watch_models(profile.id, profile.match.policy, profile.match.aliases)
    category = profile._infer_watch_category()
    is_gem = bool("gem" in profile.id or getattr(profile, "category", "") == "gems")
    poll_minutes, poll_seconds = resolve_poll_timing(
        profile.poll_seconds, profile.poll_minutes, profile.priority
    )
    notify_mode = resolve_notify_mode(profile.mode)
    seller_filter = resolve_seller_filter(profile.seller, profile.search_url)

    return WatchItem(
        id=profile.id,
        category=category,
        models=models,
        max_price=profile.max_price,
        seller=seller_filter,
        poll_minutes=poll_minutes,
        poll_seconds=poll_seconds,
        search_urls=[profile.search_url] if profile.search_url else [],
        enabled=(profile.mode != "off"),
        is_gem_feed=is_gem,
        priority=profile.priority,
        notify_mode=notify_mode,
        price_headroom_pct=profile.headroom_pct,
        owner_user_id=profile.owner_user_id,
    )


