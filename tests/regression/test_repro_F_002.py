"""Regression test for F-002: PriceWatchWorker initialized without client in bootstrap.py.

Demonstrates that bootstrap.py initializes PriceWatchWorker with client=None,
causing all price checks to return error='no_client'.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from hunter.bootstrap import Bootstrap, HunterBootstrap
from hunter.core.config import Settings
from hunter.core.db import Database


@pytest.mark.asyncio
async def test_price_watch_worker_has_http_client(tmp_path: Path) -> None:
    settings = Settings()
    settings.public_v2.enabled = True
    settings.public_v2.price_watch_enabled = True

    db_path = tmp_path / "hunter_test.db"
    db = Database(db_path)

    bootstrap = Bootstrap(settings, db=db, senders={"telegram": AsyncMock()})
    await bootstrap.startup()

    worker = bootstrap.price_watch_worker
    assert worker is not None
    assert worker.fetcher is not None
    assert worker.fetcher.client is not None, (
        "Expected price_watch_worker.fetcher.client to be initialized, but got None"
    )

    await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_hunter_bootstrap_alias_and_price_fetch_oom_protection(tmp_path: Path) -> None:
    settings = Settings()
    settings.public_v2.enabled = True
    settings.public_v2.price_watch_enabled = True

    db_path = tmp_path / "hunter_test_alias.db"
    db = Database(db_path)

    bootstrap = HunterBootstrap(settings, db=db, senders={"telegram": AsyncMock()})
    await bootstrap.startup()

    assert bootstrap.price_watch_worker.fetcher.client is not None
    assert bootstrap.price_watch_worker.fetcher.max_body_bytes == 500 * 1024

    await bootstrap.shutdown()
