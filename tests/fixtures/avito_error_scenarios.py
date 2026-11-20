"""Reusable test fixtures and scenario responses for Avito HTTP pipeline error tests (T-982)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.slots import SlotHealth

SAMPLE_HTML = """
<!DOCTYPE html><html><body>
<div data-marker="item" data-item-id="{item_id}">
  <a data-marker="item-title" href="/items/{item_id}"><h3>Item {item_id}</h3></a>
  <meta itemprop="price" content="{price}">
  <span class="price-text">{price} ₽</span>
</div>
</body></html>
"""

CONTRADICTORY_EMPTY_JSON = '{"status": "ok", "result": {"total": 42, "items": []}}'


def make_feed_html(item_id: str = "1112223334", price: int = 9900) -> str:
    return SAMPLE_HTML.format(item_id=item_id, price=price)


def make_scenario_response(
    scenario: str,
    request: httpx.Request,
    item_id: str = "1112223334",
    price: int = 9900,
) -> httpx.Response:
    if scenario == "200":
        return httpx.Response(200, text=make_feed_html(item_id, price), request=request)
    if scenario == "403":
        return httpx.Response(403, text="Forbidden", request=request)
    if scenario == "429":
        return httpx.Response(429, headers={"Retry-After": "900"}, text="Too Many Requests", request=request)
    if scenario == "439":
        html = '<html><body>firewall/captcha window.pow_challenge="e30.eyJpZCI6ICJhIiwgImNvbXBsIjogMX0=.sig"</body></html>'
        return httpx.Response(439, text=html, request=request)
    if scenario in ("500", "502", "503"):
        return httpx.Response(int(scenario), text=f"HTTP {scenario}", request=request)
    if scenario == "empty_json":
        return httpx.Response(200, text=CONTRADICTORY_EMPTY_JSON, request=request)
    if scenario == "explicit_empty":
        return httpx.Response(200, text='<html><body><div data-marker="catalog-empty">Ничего не найдено</div></body></html>', request=request)
    raise ValueError(f"Unknown scenario: {scenario}")


async def _count_rows(db: Database, query: str) -> int:
    return int((await (await db._conn.execute(query)).fetchone())[0])


async def seed_restart_durability_entities(bootstrap: Bootstrap, slot_id: str, db_path: Path) -> tuple[int, str]:
    now_iso = datetime.now(UTC).isoformat()
    await bootstrap.writer.submit(lambda c: c.execute("INSERT INTO users (id, status, created_at) VALUES (8999, 'active', ?)", (now_iso,)))
    await bootstrap.cutover_mgr.set_tenant_mode(8999, CutoverMode.V2)
    sub = await bootstrap.subscription_service.add_subscription(8999, "https://www.avito.ru/moskva?q=phone", 10_000)
    payload = json.dumps({"avito_id": "av_111", "new_price": 8000, "title": "Phone", "url": "https://avito.ru/1"})
    async def _seed(c):
        await c.execute("UPDATE queries SET bootstrap_state = 'ready' WHERE id = ?", (sub.query_id,))
        await c.execute("INSERT INTO query_membership (query_id, avito_id, first_seen, last_seen) VALUES (?, 'av_111', ?, ?)", (sub.query_id, now_iso, now_iso))
        await c.execute("INSERT INTO events (id, avito_id, revision, kind, observed_at, old_price, new_price) VALUES ('ev_rst_1', 'av_111', 1, 'PRICE_CHANGED', ?, 10000, 8000)", (now_iso,))
        await c.execute("INSERT INTO recipient_events (user_id, event_id) VALUES (8999, 'ev_rst_1')")
        await c.execute("INSERT INTO deliveries (id, event_id, user_id, channel, available_at, status, payload) VALUES ('del_rst_1', 'ev_rst_1', 8999, 'telegram', ?, 'pending', ?)", (now_iso, payload))
    await bootstrap.writer.submit(_seed)
    return sub.query_id, "ev_rst_1"


async def verify_restored_bootstrap_state(db_path: Path, slot_id: str) -> None:
    new_settings = Settings(env="test", data_dir=str(db_path.parent))
    new_settings.public_v2.enabled = True
    new_db = Database(db_path)
    new_sender = AsyncMock(return_value=SendResult("sent", "msg-id"))
    new_bootstrap = Bootstrap(new_settings, db=new_db, senders={"telegram": new_sender})
    await new_bootstrap.startup()
    try:
        await new_bootstrap.dispatcher.stop()
        restored_slot = await new_bootstrap.slot_repo.get_slot(slot_id)
        assert restored_slot is not None
        if restored_slot.consecutive_errors > 0:
            assert restored_slot.consecutive_errors == 2 and restored_slot.health == SlotHealth.QUARANTINED
        assert await _count_rows(new_db, "SELECT COUNT(*) FROM deliveries WHERE status = 'pending'") == 1
        assert await _count_rows(new_db, "SELECT COUNT(*) FROM events") == 1
        assert await new_bootstrap.dispatcher.dispatch_once() == 1
        new_sender.assert_awaited_once()

        staging_path = Path(new_bootstrap.session_store.base_dir) / ".tmp_slot_crash.json"
        assert not staging_path.exists()
    finally:
        await new_bootstrap.shutdown()


def child_process_crash_work(db_file: str, session_dir: str) -> None:
    import asyncio, os
    from pathlib import Path
    import httpx

    assert "tmp" in os.environ.get("HUNTER_DATA_DIR", "").lower()

    async def _run():
        new_settings = Settings(env="test", data_dir=str(Path(db_file).parent))
        new_settings.public_v2.enabled = True
        new_settings.public_v2.browser_fetch = False
        bootstrap = Bootstrap(new_settings, db=Database(Path(db_file)), senders={"telegram": lambda m: None})
        await bootstrap.startup()
        bootstrap.http_pool.get_fetcher(
            "slot_crash", "direct",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>")),
        )
        await bootstrap.slot_repo.register_slot("slot_crash", "direct")
        await seed_restart_durability_entities(bootstrap, "slot_crash", Path(db_file))

        staging_path = Path(bootstrap.session_store.base_dir) / ".tmp_slot_crash.json"
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text('{"uncommitted_staging": true}')
        os._exit(42)

    asyncio.run(_run())
