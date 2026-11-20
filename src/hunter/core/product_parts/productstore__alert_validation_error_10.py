from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from hunter.core.models import now_utc

_DEFAULT_STORE: Any = None


def get_default_store() -> Any:
    global _DEFAULT_STORE
    return _DEFAULT_STORE


def set_default_store(store: Any) -> None:
    global _DEFAULT_STORE
    _DEFAULT_STORE = store
    import sys
    if "hunter.core.product" in sys.modules:
        sys.modules["hunter.core.product"]._DEFAULT_STORE = store


def alert_validation_error(
    store: Any,
    product_id: str,
    error: str,
    notifier: Any,
) -> None:
    now = now_utc()
    last_alert = store._last_alert_ts.get(product_id)
    if last_alert and (now - last_alert).total_seconds() < 6 * 3600:
        return  # 6 hour cooldown
    store._last_alert_ts[product_id] = now
    try:
        import asyncio
        msg = f"⚠ Ошибка валидации профиля товара '{product_id}': {error}\nИспользуется последняя рабочая версия."
        if asyncio.iscoroutinefunction(getattr(notifier, "alert", None)):
            asyncio.create_task(notifier.alert(msg))
        elif callable(getattr(notifier, "alert", None)):
            notifier.alert(msg)
    except Exception as exc:
        logger.debug("Could not send validation error alert: {}", exc)


def resolve_expected_product_dir() -> Path:
    try:
        import os
        if os.environ.get("HUNTER_DATA_DIR"):
            return Path(os.environ["HUNTER_DATA_DIR"]) / "products"
        from hunter.core.config import get_settings
        return get_settings().data_dir / "products"
    except Exception:
        return Path(__file__).resolve().parents[4] / "data" / "products"


def get_product_store(directory: Path | None = None) -> Any:
    from hunter.core.product import ProductStore

    if directory is not None:
        return ProductStore(directory)

    expected_dir = resolve_expected_product_dir()
    current_store = get_default_store()

    if current_store is None or getattr(current_store, "directory", None) != expected_dir:
        new_store = ProductStore(expected_dir)
        new_store.load_all()
        set_default_store(new_store)
        return new_store
    else:
        current_store.check_reload()
        return current_store
