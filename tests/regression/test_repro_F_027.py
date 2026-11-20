"""Regression test for F-027: SessionStore unhandled EndpointMismatchError on proxy rotation."""

from __future__ import annotations

import tempfile
import pytest

from hunter.transport.http_fetch import HttpSlotFetcher
from hunter.transport.session_store import SessionStore


@pytest.mark.asyncio
async def test_repro_F_027_fetcher_handles_endpoint_rotation_gracefully() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = SessionStore(base_dir=tmp_dir)
        old_endpoint = "http://192.168.1.10:8080"
        new_endpoint = "http://192.168.1.20:8080"
        slot_id = "slot_test"

        # 1. Existing session saved for old proxy endpoint
        await store.save_session(
            slot_id=slot_id,
            endpoint=old_endpoint,
            version=1,
            generation=1,
            cookies=[{"name": "session_id", "value": "abc123xyz", "domain": ".avito.ru"}],
        )

        # 2. Proxy endpoint is rotated on slot
        fetcher = HttpSlotFetcher(
            slot_id=slot_id,
            endpoint=new_endpoint,
            session_store=store,
        )

        try:
            # Expected behavior: should not crash with EndpointMismatchError;
            # should gracefully reset/invalidate old cookies and continue.
            loaded = await fetcher.load_cookies_from_store()
            assert loaded == 0
        finally:
            await fetcher.close()


@pytest.mark.asyncio
async def test_repro_F_027_import_session_fd_handles_endpoint_rotation() -> None:
    import json
    import os
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = SessionStore(base_dir=tmp_dir)
        old_endpoint = "http://192.168.1.10:8080"
        new_endpoint = "http://192.168.1.20:8080"
        slot_id = "slot_test"

        await store.save_session(
            slot_id=slot_id,
            endpoint=old_endpoint,
            version=1,
            generation=1,
            cookies=[{"name": "session_id", "value": "old_token", "domain": ".avito.ru"}],
        )

        payload = json.dumps({
            "cookies": [{"name": "session_id", "value": "new_token", "domain": ".avito.ru"}]
        }).encode("utf-8")
        r_fd, w_fd = os.pipe()
        os.write(w_fd, payload)
        os.close(w_fd)

        meta = await store.import_session_fd(
            fd=r_fd,
            slot_id=slot_id,
            endpoint=new_endpoint,
            version=2,
            generation=1,
        )
        assert meta.endpoint == new_endpoint
        assert meta.version >= 2
