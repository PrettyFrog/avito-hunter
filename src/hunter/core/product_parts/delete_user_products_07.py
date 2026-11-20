from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
import yaml

from hunter.core.config import get_settings


def _collect_disk_product_ids(base_dir: Path, user_id: int) -> set[str]:
    product_ids: set[str] = set()
    if not base_dir.exists():
        return product_ids
    for yf in base_dir.glob("*.yaml"):
        if yf.name.endswith(".tmp") or yf.name.startswith("."):
            continue
        try:
            raw = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict) and raw.get("owner_user_id") == user_id:
                product_ids.add(raw.get("id") or yf.stem)
        except Exception as exc:
            logger.debug("Failed checking product file {} for user {}: {}", yf.name, user_id, exc)
    return product_ids


async def _cleanup_db_product_records(db: Any, user_product_ids: set[str]) -> None:
    if db is None or not user_product_ids:
        return
    conn = await db.connect()
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in await cur.fetchall()}
    for pid in user_product_ids:
        if "jobs" in tables:
            await conn.execute("DELETE FROM jobs WHERE product_id = ?", (pid,))
        if "products" in tables:
            await conn.execute("DELETE FROM products WHERE id = ?", (pid,))
        if "llm_calls" in tables:
            await conn.execute("UPDATE llm_calls SET product_id = NULL WHERE product_id = ?", (pid,))
        if "photo_shadow_results" in tables:
            await conn.execute("DELETE FROM photo_shadow_results WHERE product_id = ?", (pid,))
    await conn.commit()


async def delete_user_products(
    user_id: int,
    *,
    directory: Path | str | None = None,
    store: Any = None,
    db: Any = None,
) -> list[str]:
    """Finds all products belonging to user_id by owner_user_id (never by filename pattern).
    Tombstones them, deletes YAML and ref directories, deletes jobs from jobs table,
    cancels pending notifications/digest items.
    Owner products cannot be deleted.
    """
    if db is not None:
        owner = await db.get_owner_user()
        if owner and owner.get("id") == user_id:
            raise ValueError("Owner cannot be deleted, blocked, or paused")

    from hunter.core.product import delete_product_profile, get_product_store

    dest = directory or getattr(store, "directory", None) or (get_settings().data_dir / "products")
    base_dir = Path(dest).resolve()
    user_product_ids: set[str] = set()

    active_store = store or get_product_store()
    if active_store:
        for pid, p in list(active_store.profiles.items()):
            if p.owner_user_id == user_id:
                user_product_ids.add(pid)

    user_product_ids |= _collect_disk_product_ids(base_dir, user_id)

    deleted_ids: list[str] = []
    for pid in sorted(user_product_ids):
        try:
            delete_product_profile(pid, directory=base_dir)
            deleted_ids.append(pid)
        except Exception as exc:
            logger.error("Failed to delete user product profile {}: {}", pid, exc)

    await _cleanup_db_product_records(db, user_product_ids)
    return deleted_ids


def stretch_user_product_intervals(
    user_id: int,
    min_interval_minutes: int,
    *,
    directory: Path | str | None = None,
    store: Any = None,
) -> int:
    """Adjusts existing user products so that poll_minutes >= min_interval_minutes.
    Saves updated profiles to YAML.
    Returns number of updated products.
    """
    from hunter.core.product import get_product_store, save_product_profile

    dest = directory or getattr(store, "directory", None) or (get_settings().data_dir / "products")
    base_dir = Path(dest).resolve()
    active_store = store or get_product_store()
    profiles, _ = active_store.load_all()

    updated_count = 0
    min_seconds = min_interval_minutes * 60
    for p in profiles.values():
        if p.owner_user_id == user_id:
            cur_minutes = p.poll_minutes or 0
            cur_seconds = p.poll_seconds or 0
            if cur_minutes < min_interval_minutes or cur_seconds < min_seconds:
                p.poll_minutes = max(cur_minutes, min_interval_minutes)
                p.poll_seconds = max(cur_seconds, min_seconds)
                save_product_profile(p, directory=base_dir)
                updated_count += 1
    return updated_count


def resolve_product_store_directory(
    directory: Path | None = None,
    products_dir: Path | None = None,
) -> Path:
    target_dir = directory or products_dir
    if target_dir is not None:
        return Path(target_dir)
    try:
        import os
        if os.environ.get("HUNTER_DATA_DIR"):
            return Path(os.environ["HUNTER_DATA_DIR"]) / "products"
        s_dir = get_settings().data_dir / "products"
        if s_dir.exists():
            return s_dir
        return Path(__file__).resolve().parents[4] / "data" / "products"
    except Exception:
        return Path(__file__).resolve().parents[4] / "data" / "products"


def init_product_store(
    store: Any,
    directory: Path | None = None,
    products_dir: Path | None = None,
) -> None:
    from hunter.core.product_parts.productprofile_to_watch_04 import _TOMBSTONES

    store.directory = resolve_product_store_directory(directory, products_dir)
    store._profiles = {}
    store._file_mtimes = {}
    store._last_check_ts = 0.0
    store._last_alert_ts = {}
    store._tombstones = set(_TOMBSTONES)


def product_store_get(store: Any, product_id: str) -> Any:
    if not store._profiles:
        store.load_all()
    return store._profiles.get(product_id)


def product_store_get_for_user(store: Any, user_id: int) -> dict[str, Any]:
    """Returns all product profiles belonging to the given user."""
    if not store._profiles:
        store.load_all()
    return {pid: p for pid, p in store._profiles.items() if p.owner_user_id == user_id}


def product_store_save(store: Any, profile: Any) -> Path:
    """Saves product profile to disk and in-memory cache. Rejects tombstoned products."""
    from hunter.core.product_parts.productprofile_to_watch_04 import (
        is_tombstoned,
        save_product_profile,
    )

    if profile.id in store._tombstones or is_tombstoned(profile.id):
        raise ValueError(f"Cannot save tombstoned product '{profile.id}'")
    target_path = save_product_profile(profile, directory=store.directory)
    store._profiles[profile.id] = profile
    try:
        store._file_mtimes[str(target_path)] = target_path.stat().st_mtime
    except Exception:
        pass
    return target_path


def product_store_delete(store: Any, product_id: str) -> bool:
    """Deletes product profile from disk and in-memory cache."""
    from hunter.core.product_parts.productprofile_to_watch_04 import delete_product_profile

    store._tombstones.add(product_id)
    deleted = delete_product_profile(product_id, directory=store.directory)
    store._profiles.pop(product_id, None)
    store._file_mtimes.pop(str(store.directory / f"{product_id}.yaml"), None)
    return deleted


def _load_single_product_file(
    store: Any,
    yf: Path,
    errors: list[str],
    notifier: Any = None,
) -> None:
    from hunter.core.product import ProductProfile

    try:
        content = yf.read_text(encoding="utf-8")
        raw = yaml.safe_load(content) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"File {yf.name} must contain a YAML mapping")
        profile = ProductProfile.model_validate(raw)
        store._profiles[profile.id] = profile
        store._file_mtimes[str(yf)] = yf.stat().st_mtime
    except Exception as exc:
        err_msg = f"Failed to load product {yf.name}: {exc}"
        errors.append(err_msg)
        logger.error(err_msg)
        if notifier:
            store._alert_validation_error(yf.stem, err_msg, notifier)


def product_store_load_all(
    store: Any,
    notifier: Any = None,
) -> tuple[dict[str, Any], list[str]]:
    """Scans data/products/*.yaml, validates profiles, and alerts on errors without dropping working ones."""
    from hunter.core.product_parts.productprofile_to_watch_04 import is_tombstoned

    errors: list[str] = []
    if not store.directory.exists():
        return store._profiles, errors

    yaml_files = sorted(store.directory.glob("*.yaml"))
    current_seen: set[str] = set()

    for yf in yaml_files:
        if yf.name.endswith(".tmp") or yf.name.startswith("."):
            continue
        if yf.stem in store._tombstones or is_tombstoned(yf.stem):
            logger.debug("Skipping tombstoned product profile file: {}", yf.name)
            try:
                yf.unlink()
            except Exception:
                pass
            continue
        current_seen.add(yf.stem)
        _load_single_product_file(store, yf, errors, notifier)

    # Remove deleted profiles if file no longer exists
    for pid in list(store._profiles.keys()):
        if pid not in current_seen and not (store.directory / f"{pid}.yaml").exists():
            logger.info("Product profile {} removed from disk", pid)
            store._profiles.pop(pid, None)

    return store._profiles, errors


def _has_file_mtimes_changed(store: Any, yaml_files: list[Path]) -> bool:
    for yf in yaml_files:
        if yf.name.endswith(".tmp") or yf.name.startswith("."):
            continue
        try:
            mtime = yf.stat().st_mtime
            if str(yf) not in store._file_mtimes or store._file_mtimes[str(yf)] != mtime:
                return True
        except Exception:
            pass
    return False


def product_store_check_reload(
    store: Any,
    notifier: Any = None,
    force: bool = False,
) -> bool:
    """Hot reload: checks file modifications if > 5s elapsed (guarantee <= 60s)."""
    import time
    now = time.time()
    if not force and (now - store._last_check_ts) < 5.0:
        return False
    store._last_check_ts = now

    if not store.directory.exists():
        return False

    yaml_files = sorted(store.directory.glob("*.yaml"))
    changed = _has_file_mtimes_changed(store, yaml_files)

    if changed or force or len(yaml_files) != len(store._profiles):
        _, errors = store.load_all(notifier=notifier)
        return len(errors) == 0
    return False


def product_store_as_watch_items(store: Any) -> list[Any]:
    return [p.to_watch_item() for p in store._profiles.values()]


