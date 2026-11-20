from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from loguru import logger
import yaml

from hunter.core.config import get_settings
from hunter.core.models import WatchItem
from hunter.domain.delivery_policy import PAUSE_HIERARCHY, can_resume

ALLOWED_PROFILE_KEYS = {
    "id",
    "name",
    "search_url",
    "max_price",
    "min_price",
    "headroom_pct",
    "priority",
    "mode",
    "match",
    "requirements",
    "preset",
    "red_flags_extra",
    "photos",
    "coverage",
    "limits",
    "seller",
    "poll_minutes",
    "poll_seconds",
    "owner_user_id",
    "paused_reason",
    "created_at",
    "last_confirmed_at",
    "stale_prompted_at",
}

# --- Tombstone Management (M5) ---
_TOMBSTONES: set[str] = set()


def add_tombstone(product_id: str, default_store: Any = None) -> None:
    """Marks a product ID as tombstoned. It can never be saved or loaded again."""
    _TOMBSTONES.add(product_id)
    if default_store is not None:
        default_store._tombstones.add(product_id)


def is_tombstoned(product_id: str) -> bool:
    """Checks whether a product ID has been tombstoned."""
    return product_id in _TOMBSTONES


def clear_tombstones(default_store: Any = None) -> None:
    """Clears all tombstones (mainly for test isolation)."""
    _TOMBSTONES.clear()
    if default_store is not None:
        default_store._tombstones.clear()


# --- Pause Hierarchy (M5) ---
# Priority: owner_paused (3) > bot_blocked (2) > user_paused (1)
PAUSE_PRIORITY = PAUSE_HIERARCHY


def apply_product_pause(profile: Any, reason: str, actor_role: str = "user") -> bool:
    """Applies pause to product respecting priority hierarchy.
    Higher priority pauses cannot be overridden by lower priority pauses.
    Returns True if pause was applied, False if rejected.
    """
    existing_reason = profile.paused_reason
    existing_prio = PAUSE_PRIORITY.get(existing_reason or "", 0)
    new_prio = PAUSE_PRIORITY.get(reason, 0)

    # If current pause is higher priority, reject
    if existing_prio > new_prio:
        logger.debug(
            "Cannot downgrade pause on '{}' from '{}' (prio {}) to '{}' (prio {})",
            profile.id,
            existing_reason,
            existing_prio,
            reason,
            new_prio,
        )
        return False

    profile.mode = "off"
    profile.paused_reason = reason
    return True


def profile_to_watch(profile: Any) -> WatchItem:
    """Helper for ProductProfile.to_watch."""
    return profile.to_watch_item()


def clear_product_pause(
    profile: Any,
    actor_role: str = "user",
    pause_type: str | None = None,
) -> tuple[bool, str]:
    """Clears pause on product respecting priority hierarchy.
    actor_role: 'owner' can clear any pause.
                'user' can only clear 'user_paused'.
    pause_type: if specified, only clears pause if profile.paused_reason == pause_type.
    Returns (success, message).
    """
    existing_reason = profile.paused_reason
    if not existing_reason or profile.mode != "off":
        profile.mode = "live"
        profile.paused_reason = None
        return True, "Товар уже активен."

    if pause_type is not None:
        if existing_reason != pause_type:
            return False, f"Причина паузы не {pause_type}"
        profile.mode = "live"
        profile.paused_reason = None
        return True, "Поиск возобновлен."

    if not can_resume(existing_reason, actor_role) and existing_reason == "owner_paused":
        return False,(
            'Товар приостановлен администратором и не может быть возобновлен '
            'пользователем.'
        )

    if not can_resume(existing_reason, actor_role) and existing_reason == "bot_blocked":
        from hunter.notify.messages_ru import RESUME_BOT_BLOCKED_FORBIDDEN
        return False, RESUME_BOT_BLOCKED_FORBIDDEN

    profile.mode = "live"
    profile.paused_reason = None
    return True, "Поиск возобновлен."


def save_product_profile(
    arg1: Any,
    arg2: Any = None,
    *,
    directory: Path | str | None = None,
    default_store: Any = None,
) -> Path:
    """Atomic write: writes YAML to .tmp file then renames. Refuses if tombstoned."""
    from hunter.core.product import ProductProfile

    if isinstance(arg1, ProductProfile):
        profile = arg1
        dest = directory or arg2 or (get_settings().data_dir / "products")
    elif isinstance(arg2, ProductProfile):
        profile = arg2
        dest = directory or arg1
    else:
        raise ValueError("save_product_profile requires a ProductProfile")

    if is_tombstoned(profile.id):
        raise ValueError(f"Cannot save tombstoned product '{profile.id}'")

    dest_path = Path(dest)
    if dest_path.suffix in (".yaml", ".yml"):
        target_file = dest_path
        target_dir = dest_path.parent
    else:
        target_dir = dest_path
        target_file = target_dir / f"{profile.id}.yaml"

    target_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = target_dir / f"{profile.id}.yaml.tmp.{os.getpid()}"

    raw_data = profile.model_dump()
    data = {k: v for k, v in raw_data.items() if k in ALLOWED_PROFILE_KEYS}
    with open(tmp_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    os.replace(tmp_file, target_file)
    logger.debug("atomically saved product profile {} to {}", profile.id, target_file)

    if default_store is not None:
        default_store._profiles[profile.id] = profile

    return target_file


def _remove_profile_files(yaml_file: Path, ref_dir: Path, base_dir: Path) -> bool:
    deleted = False
    if yaml_file.exists():
        try:
            yaml_file.unlink()
            deleted = True
            logger.info("Deleted product profile file: {}", yaml_file)
        except Exception as exc:
            logger.error("Failed to delete product file {}: {}", yaml_file, exc)

    if ref_dir.exists() and ref_dir.is_dir():
        if not ref_dir.is_relative_to(base_dir) or ref_dir == base_dir:
            raise ValueError(f"Path traversal detected in ref dir: '{ref_dir}'")
        try:
            shutil.rmtree(ref_dir)
            logger.info("Deleted product ref directory: {}", ref_dir)
        except Exception as exc:
            logger.error("Failed to delete product ref dir {}: {}", ref_dir, exc)

    return deleted


def delete_product_profile(
    product_id: str,
    *,
    directory: Path | str | None = None,
    default_store: Any = None,
) -> bool:
    """Deletes product profile YAML and associated reference photos directory.
    Strictly prevents path traversal and registers a tombstone so product cannot be re-saved.
    Also removes the profile from the default ProductStore if loaded.
    Returns True if product YAML was deleted, False if it did not exist.
    """
    if not product_id or "/" in product_id or "\\" in product_id or ".." in product_id:
        raise ValueError(f"Invalid product ID (path traversal detected): '{product_id}'")

    dest = directory or (get_settings().data_dir / "products")
    base_dir = Path(dest).resolve()
    yaml_file = (base_dir / f"{product_id}.yaml").resolve()
    ref_dir = (base_dir / product_id).resolve()

    try:
        yaml_file.relative_to(base_dir)
        ref_dir.relative_to(base_dir)
    except ValueError:
        raise ValueError(f"Path traversal detected: '{product_id}' resolves outside '{base_dir}'")

    if yaml_file == base_dir or ref_dir == base_dir:
        raise ValueError(f"Invalid product ID resolves to root directory: '{product_id}'")

    add_tombstone(product_id)
    deleted = _remove_profile_files(yaml_file, ref_dir, base_dir)

    if default_store is not None:
        default_store._profiles.pop(product_id, None)
        default_store._file_mtimes.pop(str(yaml_file), None)
        default_store._tombstones.add(product_id)

    return deleted


