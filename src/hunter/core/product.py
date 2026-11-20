from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from hunter.core.models import WatchItem
from hunter.core.product_parts.productconflict_01 import (
    ProductConflict,
    ProductCoverage,
    ProductLimits,
    ProductMatch,
    ProductPhotos,
    build_watch_item_from_profile,
    infer_watch_category_for_profile,
    normalize_profile_post_init,
    normalize_search_url_validator,
    populate_name_validator,
    validate_id_validator,
)
from hunter.core.product_parts.productprofile_to_watch_04 import (
    ALLOWED_PROFILE_KEYS,
    PAUSE_PRIORITY,
    _TOMBSTONES,
    add_tombstone as _add_tombstone,
    apply_product_pause,
    clear_product_pause,
    clear_tombstones as _clear_tombstones,
    delete_product_profile as _delete_product_profile,
    is_tombstoned,
    profile_to_watch,
    save_product_profile as _save_product_profile,
)
from hunter.core.product_parts.delete_user_products_07 import (
    delete_user_products,
    init_product_store,
    product_store_as_watch_items,
    product_store_check_reload,
    product_store_delete,
    product_store_get,
    product_store_get_for_user,
    product_store_load_all,
    product_store_save,
    stretch_user_product_intervals,
)
from hunter.core.product_parts.productstore__alert_validation_error_10 import (
    _DEFAULT_STORE,
    alert_validation_error,
    get_default_store,
    get_product_store,
    set_default_store,
)

__all__ = [
    "ALLOWED_PROFILE_KEYS",
    "PAUSE_PRIORITY",
    "ProductConflict",
    "ProductCoverage",
    "ProductLimits",
    "ProductMatch",
    "ProductPhotos",
    "ProductProfile",
    "ProductStore",
    "_DEFAULT_STORE",
    "_TOMBSTONES",
    "add_tombstone",
    "alert_validation_error",
    "apply_product_pause",
    "clear_product_pause",
    "clear_tombstones",
    "delete_product_profile",
    "delete_user_products",
    "get_default_store",
    "get_product_store",
    "is_tombstoned",
    "profile_to_watch",
    "save_product_profile",
    "set_default_store",
    "stretch_user_product_intervals",
]


class ProductProfile(BaseModel):
    id: str
    name: str = ""
    search_url: str
    max_price: int = Field(gt=0)
    min_price: int | None = None
    headroom_pct: float = 0.20
    priority: Literal["high", "normal", "low"] = "normal"
    mode: Literal["live", "shadow", "off"] = "live"
    match: ProductMatch = Field(default_factory=ProductMatch)
    requirements: str = ""
    preset: Literal["generic", "electronics", "shoes_clothes", "furniture", "tools"] = "generic"
    red_flags_extra: list[str] = Field(default_factory=list)
    photos: ProductPhotos = Field(default_factory=ProductPhotos)
    coverage: ProductCoverage = Field(default_factory=ProductCoverage)
    limits: ProductLimits = Field(default_factory=ProductLimits)
    seller: Literal["private", "any"] | None = None
    poll_minutes: int | None = None
    poll_seconds: int | None = None
    owner_user_id: int | None = None
    paused_reason: str | None = None
    created_at: str | None = None
    last_confirmed_at: str | None = None
    stale_prompted_at: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def populate_name(cls, v: Any, info: Any) -> str:
        return populate_name_validator(v, info)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return validate_id_validator(v)

    @field_validator("search_url")
    @classmethod
    def normalize_search_url(cls, v: str) -> str:
        return normalize_search_url_validator(v)

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        normalize_profile_post_init(self)

    def _infer_watch_category(self) -> str:
        return infer_watch_category_for_profile(
            self.id,
            self.name,
            self.match.aliases,
            self.match.loose_words,
            self.preset,
        )

    def to_watch_item(self) -> WatchItem:
        """Adapter: map universal ProductProfile to existing pipeline WatchItem."""
        return build_watch_item_from_profile(self)

    def to_watch(self) -> WatchItem:
        return profile_to_watch(self)


def add_tombstone(product_id: str) -> None:
    """Marks a product ID as tombstoned. It can never be saved or loaded again."""
    _add_tombstone(product_id, get_default_store())


def clear_tombstones() -> None:
    """Clears all tombstones (mainly for test isolation)."""
    _clear_tombstones(get_default_store())


def save_product_profile(
    arg1: Any,
    arg2: Any = None,
    *,
    directory: Path | str | None = None,
) -> Path:
    """Atomic write: writes YAML to .tmp file then renames. Refuses if tombstoned."""
    return _save_product_profile(
        arg1,
        arg2,
        directory=directory,
        default_store=get_default_store(),
    )


def delete_product_profile(
    product_id: str,
    *,
    directory: Path | str | None = None,
) -> bool:
    """Deletes product profile YAML and associated reference photos directory.
    Strictly prevents path traversal and registers a tombstone so product cannot be re-saved.
    Also removes the profile from the default ProductStore if loaded.
    Returns True if product YAML was deleted, False if it did not exist.
    """
    return _delete_product_profile(
        product_id,
        directory=directory,
        default_store=get_default_store(),
    )


class ProductStore:
    """Manages loading, validation, hot reload (<= 60s), and fallback for product profiles."""

    def __init__(self, directory: Path | None = None, products_dir: Path | None = None) -> None:
        init_product_store(self, directory=directory, products_dir=products_dir)

    @property
    def profiles(self) -> dict[str, ProductProfile]:
        return self._profiles

    def get(self, product_id: str) -> ProductProfile | None:
        return product_store_get(self, product_id)

    def get_for_user(self, user_id: int) -> dict[str, ProductProfile]:
        """Returns all product profiles belonging to the given user."""
        return product_store_get_for_user(self, user_id)

    def save(self, profile: ProductProfile) -> Path:
        """Saves product profile to disk and in-memory cache. Rejects tombstoned products."""
        return product_store_save(self, profile)

    def delete(self, product_id: str) -> bool:
        """Deletes product profile from disk and in-memory cache."""
        return product_store_delete(self, product_id)

    def load_all(self, notifier: Any = None) -> tuple[dict[str, ProductProfile], list[str]]:
        """Scans data/products/*.yaml, validates profiles, and alerts on errors without dropping working ones."""
        return product_store_load_all(self, notifier=notifier)

    def check_reload(self, notifier: Any = None, force: bool = False) -> bool:
        """Hot reload: checks file modifications if > 5s elapsed (guarantee <= 60s)."""
        return product_store_check_reload(self, notifier=notifier, force=force)

    def reload_if_changed(self, notifier: Any = None, force: bool = False) -> bool:
        return self.check_reload(notifier=notifier, force=force)

    def as_watch_items(self) -> list[WatchItem]:
        return product_store_as_watch_items(self)

    def to_watch_items(self) -> list[WatchItem]:
        return self.as_watch_items()

    def _alert_validation_error(self, product_id: str, error: str, notifier: Any) -> None:
        alert_validation_error(self, product_id, error, notifier)

