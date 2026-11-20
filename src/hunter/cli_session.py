"""Owner-only protected-descriptor session import commands."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import typer

from hunter.transport.session_store import SessionStoreError
from hunter.transport.proxy_credentials import ProxyCredentialError


def _run_admin(operation: Callable[[Any], Awaitable[Any]]) -> Any:
    async def _run() -> Any:
        from hunter.bootstrap import Bootstrap
        from hunter.core.config import get_settings

        settings = get_settings().model_copy(deep=True)
        settings.public_v2.enabled = False
        bootstrap = Bootstrap(settings)
        await bootstrap.startup()
        try:
            return await operation(bootstrap)
        finally:
            await bootstrap.shutdown()

    return asyncio.run(_run())


def import_slot_session(
    slot_id: str = typer.Argument(...),
    session_fd: int = typer.Option(0, "--session-fd", min=0),
) -> None:
    """Import Playwright storage state from an inherited protected file descriptor."""

    async def _import(bootstrap: Any) -> Any:
        slot = await bootstrap.slot_repo.get_slot(slot_id)
        if slot is None or bootstrap.session_store is None:
            raise SessionStoreError("Slot or session store not found")
        try:
            existing = await bootstrap.session_store.load_session(slot_id)
        except Exception:
            existing = None
        version = max(slot.session_version + 1, existing.metadata.version + 1 if existing else 1)
        metadata = await bootstrap.session_store.import_session_fd(
            fd=session_fd,
            slot_id=slot_id,
            endpoint=slot.endpoint,
            version=version,
            generation=slot.generation,
        )
        persisted = slot.session_version
        while persisted < metadata.version:
            persisted = await bootstrap.slot_repo.bump_session_version(
                slot_id, cookie_hash=metadata.cookie_hash
            )
        return metadata

    try:
        metadata = _run_admin(_import)
    except (SessionStoreError, OSError, ValueError) as exc:
        typer.echo(f"REJECTED: session import failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"IMPORTED: {slot_id} version={metadata.version}; restart the service to load it"
    )


def import_proxy_credentials(
    slot_id: str = typer.Argument(...),
    proxy_fd: int = typer.Option(0, "--proxy-fd", min=0),
) -> None:
    """Import proxy username/password JSON from a protected descriptor."""

    async def _import(bootstrap: Any) -> None:
        slot = await bootstrap.slot_repo.get_slot(slot_id)
        if slot is None or bootstrap.proxy_credential_store is None:
            raise ProxyCredentialError("Slot or credential store not found")
        bootstrap.proxy_credential_store.import_fd(slot_id, slot.endpoint, proxy_fd)

    try:
        _run_admin(_import)
    except (ProxyCredentialError, OSError, ValueError) as exc:
        typer.echo(f"REJECTED: proxy import failed ({type(exc).__name__})", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"IMPORTED: {slot_id}; enable protected_proxy_auth and restart to use it")
