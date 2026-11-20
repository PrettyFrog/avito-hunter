"""Attaches runtime stores to a browser-context factory."""

from __future__ import annotations

import inspect
from typing import Any


async def bind_factory_stores(
    factory: Any,
    session_store: Any,
    credential_store: Any,
    credentials_enabled: bool,
) -> None:
    """Binds session and proxy-credential stores when the factory supports them."""
    bindings = (
        ("bind_session_store", session_store),
        ("bind_credential_store", credential_store if credentials_enabled else None),
    )
    for name, store in bindings:
        binder = getattr(factory, name, None)
        if binder is None:
            continue
        result = binder(store)
        if inspect.isawaitable(result):
            await result
