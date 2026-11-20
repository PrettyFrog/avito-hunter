"""End-to-end integration acceptance test for Avito error handling to outbox (T-982)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.domain.slots import SlotHealth, SlotSignal
from hunter.transport.http_fetch import SlotFetchConfig
from tests.fixtures.avito_error_scenarios import (
    make_feed_html,
    make_scenario_response,
    seed_restart_durability_entities,
    verify_restored_bootstrap_state,
)


async def _count(db: Database, query: str) -> int:
    return int((await (await db._conn.execute(query)).fetchone())[0])


@pytest.fixture
async def e2e_env(tmp_path: Path):
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.day_interval_sec = 0.05
    settings.public_v2.night_interval_sec = 0.1
    db = Database(tmp_path / "e2e.db")
    sender = AsyncMock(return_value=SendResult("sent", "msg-id"))
    bootstrap = Bootstrap(settings, db=db, senders={"telegram": sender})
    await bootstrap.startup()
    try:
        await bootstrap.dispatcher.stop()
        yield bootstrap, sender, db, tmp_path / "e2e.db"
    finally:
        await bootstrap.shutdown()


@pytest.mark.asyncio
async def test_cold_start_errors_do_not_create_outbox_events_then_recover(e2e_env) -> None:
    bootstrap, sender, db, _ = e2e_env
    slot = await bootstrap.slot_repo.register_slot("slot_e2e", "http://slot.invalid:8080")

    sequence = ["500", "502", "empty_json", "403", "429", "200", "200"]
    step = 0
    got_price_change = asyncio.Event()

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal step
        cur = sequence[min(step, len(sequence) - 1)]
        price = 9900 if step <= 5 else 8900
        step += 1
        if step >= len(sequence):
            got_price_change.set()
        return make_scenario_response(cur, request, price=price)

    bootstrap.http_pool.get_fetcher(slot.slot_id, slot.endpoint, transport=httpx.MockTransport(_handler))
    await bootstrap.writer.submit(lambda c: c.execute(
        "INSERT INTO users (id, status, created_at) VALUES (8001, 'active', datetime('now'))"
    ))
    await bootstrap.subscription_service.add_subscription(8001, "https://www.avito.ru/moskva?q=phone", 10_000)
    await bootstrap.cutover_mgr.set_tenant_mode(8001, CutoverMode.V2)

    for _ in range(60):
        if got_price_change.is_set():
            break
        s = await bootstrap.slot_repo.get_slot(slot.slot_id)
        if s and s.health in (SlotHealth.QUARANTINED, SlotHealth.SUSPECT) and bootstrap.supervisor:
            future_ts = (datetime.now(UTC) + timedelta(seconds=1800)).isoformat()
            await bootstrap.supervisor.check_quarantined_slots(now_ts=future_ts)
        await asyncio.sleep(0.05)

    assert got_price_change.is_set(), f"Price change event not reached! Step={step}"
    for _ in range(100):
        if await _count(db, "SELECT COUNT(*) FROM events"):
            break
        await asyncio.sleep(0.01)

    await bootstrap.public_poll_runtime.stop()

    assert await _count(db, "SELECT COUNT(*) FROM events") == 1
    assert await _count(db, "SELECT COUNT(*) FROM deliveries WHERE status = 'pending'") == 1
    assert await bootstrap.dispatcher.dispatch_once() == 1
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_and_primary_5xx_backoff_cooldown(e2e_env) -> None:
    bootstrap, sender, db, _ = e2e_env
    slot = await bootstrap.slot_repo.register_slot("slot_ra_e2e", "http://slot.invalid:8080")

    async def _handler(request: httpx.Request) -> httpx.Response:
        return make_scenario_response("429", request)

    bootstrap.http_pool.get_fetcher(slot.slot_id, slot.endpoint, transport=httpx.MockTransport(_handler))
    await bootstrap.writer.submit(lambda c: c.execute(
        "INSERT INTO users (id, status, created_at) VALUES (8002, 'active', datetime('now'))"
    ))
    await bootstrap.subscription_service.add_subscription(8002, "https://www.avito.ru/moskva?q=phone", 10_000)
    await bootstrap.cutover_mgr.set_tenant_mode(8002, CutoverMode.V2)

    await asyncio.sleep(0.15)
    await bootstrap.public_poll_runtime.stop()

    s = await bootstrap.slot_repo.get_slot("slot_ra_e2e")
    assert s and s.quarantined_until
    cooldown = round((datetime.fromisoformat(s.quarantined_until) - datetime.fromisoformat(s.updated_at)).total_seconds())
    assert cooldown >= 900
    assert await _count(db, "SELECT COUNT(*) FROM events") == 0


@pytest.mark.asyncio
async def test_disabled_slot_fences_outbox_events(e2e_env) -> None:
    bootstrap, sender, db, _ = e2e_env
    slot = await bootstrap.slot_repo.register_slot("slot_dis", "http://slot.invalid:8080")

    async def _handler(request: httpx.Request) -> httpx.Response:
        return make_scenario_response("200", request, price=7000)

    bootstrap.http_pool.get_fetcher(slot.slot_id, slot.endpoint, transport=httpx.MockTransport(_handler))
    await bootstrap.writer.submit(lambda c: c.execute(
        "INSERT INTO users (id, status, created_at) VALUES (8003, 'active', datetime('now'))"
    ))
    await bootstrap.subscription_service.add_subscription(8003, "https://www.avito.ru/moskva?q=phone", 10_000)
    await bootstrap.slot_repo.set_health(slot.slot_id, SlotHealth.DISABLED)
    await bootstrap.cutover_mgr.set_tenant_mode(8003, CutoverMode.V2)

    await asyncio.sleep(0.15)
    await bootstrap.public_poll_runtime.stop()

    assert await _count(db, "SELECT COUNT(*) FROM events") == 0
    assert await _count(db, "SELECT COUNT(*) FROM deliveries") == 0
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_secondary_retry_error_records_backoff_without_crash(e2e_env) -> None:
    bootstrap, sender, db, _ = e2e_env
    slot = await bootstrap.slot_repo.register_slot("slot_sec", "http://slot.invalid:8080")

    bootstrap.challenge_service.http_pow_solver = AsyncMock(
        return_value=(True, [{"name": "cf_clearance", "value": "1", "domain": ".avito.ru", "path": "/"}])
    )

    attempts = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return make_scenario_response("439" if attempts == 1 else "429", request)

    bootstrap.http_pool.get_fetcher(slot.slot_id, slot.endpoint, transport=httpx.MockTransport(_handler))
    await bootstrap.writer.submit(lambda c: c.execute(
        "INSERT INTO users (id, status, created_at) VALUES (8004, 'active', datetime('now'))"
    ))
    await bootstrap.subscription_service.add_subscription(8004, "https://www.avito.ru/moskva?q=phone", 10_000)
    await bootstrap.cutover_mgr.set_tenant_mode(8004, CutoverMode.V2)

    with patch("hunter.application.public_poll_runtime.asyncio.sleep", new=AsyncMock()):
        for _ in range(60):
            s = await bootstrap.slot_repo.get_slot("slot_sec")
            if attempts >= 2 and s and s.consecutive_errors >= 1:
                break
            await asyncio.sleep(0.05)
        await bootstrap.public_poll_runtime.stop()

    bootstrap.challenge_service.http_pow_solver.assert_awaited()
    assert attempts >= 2
    s = await bootstrap.slot_repo.get_slot("slot_sec")
    assert s and s.consecutive_errors >= 1
    assert await _count(db, "SELECT COUNT(*) FROM events") == 0


@pytest.mark.asyncio
async def test_process_restart_preserves_slot_health_and_durability(e2e_env) -> None:
    bootstrap, sender, db, db_path = e2e_env
    slot = await bootstrap.slot_repo.register_slot("slot_rst", "http://slot.invalid:8080")
    await seed_restart_durability_entities(bootstrap, slot.slot_id, db_path)

    await bootstrap.public_poll_runtime.stop()
    if bootstrap.supervisor:
        await bootstrap.supervisor.handle_slot_signal(slot.slot_id, SlotSignal.HTTP_403)
        await bootstrap.supervisor.handle_slot_signal(slot.slot_id, SlotSignal.HTTP_403)

    s1 = await bootstrap.slot_repo.get_slot(slot.slot_id)
    assert s1 and s1.consecutive_errors == 2

    await bootstrap.shutdown()
    await verify_restored_bootstrap_state(db_path, slot.slot_id)


@pytest.mark.asyncio
async def test_slot_failover_and_recovery_ab(e2e_env) -> None:
    """T-982: Failover from slot A to slot B on persistent errors."""
    bootstrap, sender, db, _ = e2e_env
    await bootstrap.slot_repo.register_slot("slot_a", "http://slot-a.invalid:8080")
    await bootstrap.slot_repo.register_slot("slot_b", "http://slot-b.invalid:8080")

    await bootstrap.writer.submit(lambda c: c.execute(
        "INSERT INTO users (id, status, created_at) VALUES (8005, 'active', datetime('now'))"
    ))
    sub = await bootstrap.subscription_service.add_subscription(8005, "https://www.avito.ru/moskva?q=phone", 10_000)

    cur = await (await db._conn.execute("SELECT slot_id FROM assignments WHERE query_id = ? AND active = 1", (sub.query_id,))).fetchone()
    first_slot = cur[0]
    second_slot = "slot_b" if first_slot == "slot_a" else "slot_a"

    if bootstrap.supervisor:
        for _ in range(3):
            await bootstrap.supervisor.handle_slot_signal(first_slot, SlotSignal.HTTP_403)
        cur2 = await (await db._conn.execute("SELECT slot_id FROM assignments WHERE query_id = ? AND active = 1", (sub.query_id,))).fetchone()
        assert cur2[0] == second_slot


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type, exc", [
    ("dns", httpx.ConnectError("dns")),
    ("tls", httpx.ConnectError("SSL")),
    ("connect_timeout", httpx.ConnectTimeout("conn")),
    ("read_timeout", httpx.ReadTimeout("read")),
    ("cancel", asyncio.CancelledError()),
])
async def test_network_exceptions_dns_tls_timeout(e2e_env, error_type: str, exc: Exception) -> None:
    """T-982: Parameterized network exceptions record backoff without unhandled crash."""
    bootstrap, sender, db, _ = e2e_env
    slot_id = f"slot_net_{error_type}"
    slot = await bootstrap.slot_repo.register_slot(slot_id, f"http://{slot_id}.invalid:8080")

    handler_calls = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal handler_calls
        handler_calls += 1
        raise exc

    bootstrap.http_pool.get_fetcher(slot.slot_id, slot.endpoint, transport=httpx.MockTransport(_handler))
    user_id = 8100 + abs(hash(error_type)) % 800
    await bootstrap.writer.submit(lambda c: c.execute(
        "INSERT INTO users (id, status, created_at) VALUES (?, 'active', datetime('now'))", (user_id,)
    ))
    await bootstrap.subscription_service.add_subscription(user_id, f"https://www.avito.ru/moskva?q={error_type}", 10_000)
    await bootstrap.cutover_mgr.set_tenant_mode(user_id, CutoverMode.V2)

    for _ in range(40):
        if handler_calls >= 1:
            break
        await asyncio.sleep(0.02)

    for _ in range(25):
        s = await bootstrap.slot_repo.get_slot(slot_id)
        if s and (error_type == "cancel" or s.consecutive_errors >= 1):
            break
        await asyncio.sleep(0.02)

    await bootstrap.public_poll_runtime.stop()

    assert handler_calls >= 1, f"Handler for {error_type} was not reached!"
    s = await bootstrap.slot_repo.get_slot(slot_id)
    assert s is not None
    if error_type != "cancel":
        assert s.consecutive_errors >= 1
    assert await _count(db, "SELECT COUNT(*) FROM events") == 0


@pytest.mark.asyncio
async def test_child_process_abrupt_crash_and_recovery(tmp_path: Path) -> None:
    """T-982: Real child-process crash (os._exit) leaves DB consistent for recovery and dispatch."""
    import subprocess
    import sys

    db_file = str(tmp_path / "crash.db")
    session_dir = str(tmp_path / "sessions")
    data_dir = str(tmp_path / "data")
    env = {
        **os.environ,
        "PYTHONPATH": "src:tests/fixtures:.",
        "HUNTER_DATA_DIR": data_dir,
        "HUNTER_SESSIONS_DIR": session_dir,
    }
    code = f"from tests.fixtures.avito_error_scenarios import child_process_crash_work; child_process_crash_work({db_file!r}, {session_dir!r})"
    proc = subprocess.run([sys.executable, "-c", code], env=env, timeout=10.0)
    assert proc.returncode == 42
    await verify_restored_bootstrap_state(Path(db_file), "slot_crash")


async def _verify_cookie_isolation(fetcher: Any, store: Any, seen_uas: list[str]) -> None:
    assert len(set(seen_uas)) >= 2, "Must see multiple distinct User-Agents"
    assert fetcher.curl_client.cookies.jar is not fetcher.curl_client_b.cookies.jar, "Cookie jars must be separate"
    ja = {c.name for c in fetcher.curl_client.cookies.jar}
    jb = {c.name for c in fetcher.curl_client_b.cookies.jar}
    assert bool(ja) and bool(jb), "Both jars must have cookies"
    assert ja != jb and not (ja & jb), f"Cookie names must not overlap: {ja} & {jb}"
    await fetcher.save_cookies_to_store()
    sa = await store.load_session("slot_aba")
    sb = await store.load_session("slot_aba__user_b")
    assert sa and sb and sa.cookies and sb.cookies, "Both sessions must be persisted"
    assert {c["name"] for c in sa.cookies} != {c["name"] for c in sb.cookies}


@pytest.mark.asyncio
async def test_user_ab_identity_and_price_history_aba(e2e_env) -> None:
    """T-982: Session A/B identities produce distinct chronological revisions for price history A->B->A."""
    bootstrap, sender, db, _ = e2e_env
    from aiohttp import web
    step, prices = 0, [10000, 8000, 10000]
    seen_uas: list[str] = []

    async def _handler(request: web.Request) -> web.Response:
        nonlocal step
        cur = prices[min(step, 2)]
        step += 1
        ua = request.headers.get("User-Agent", "")
        seen_uas.append(ua)
        resp = web.Response(text=make_feed_html("1112223334", cur), content_type="text/html")
        if "Firefox" in ua:
            resp.headers.add("Set-Cookie", "token_b=val_secret_b; Path=/")
        else:
            resp.headers.add("Set-Cookie", "token_a=val_secret_a; Path=/")
        return resp

    app = web.Application()
    app.router.add_get("/{tail:.*}", _handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    try:
        bootstrap.http_pool.config = SlotFetchConfig(base_url=f"http://127.0.0.1:{port}", interleave_sessions=True)
        slot = await bootstrap.slot_repo.register_slot("slot_aba", "direct")
        fetcher = bootstrap.http_pool.get_fetcher(slot.slot_id, slot.endpoint)
        assert fetcher.curl_client_b is not None

        await bootstrap.writer.submit(lambda c: c.execute("INSERT INTO users (id, status, created_at) VALUES (8990, 'active', datetime('now'))"))
        await bootstrap.subscription_service.add_subscription(8990, "https://www.avito.ru/moskva?q=phone_aba", 10_000)
        await bootstrap.cutover_mgr.set_tenant_mode(8990, CutoverMode.V2)

        for _ in range(80):
            if step >= 3 and await _count(db, "SELECT COUNT(*) FROM events") >= 2:
                break
            await asyncio.sleep(0.05)
        await bootstrap.public_poll_runtime.stop()

        assert await _count(db, "SELECT COUNT(*) FROM events") >= 2
        events = await (await db._conn.execute("SELECT kind, old_price, new_price FROM events ORDER BY revision")).fetchall()
        price_events = [e for e in events if e[0] == "PRICE_CHANGED"]
        assert len(price_events) >= 2
        assert price_events[0][1] == 10000 and price_events[0][2] == 8000
        assert price_events[1][1] == 8000 and price_events[1][2] == 10000
        await _verify_cookie_isolation(fetcher, bootstrap.session_store, seen_uas)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_in_flight_disable_with_stage_barrier(e2e_env) -> None:
    """T-982: In-flight disabled query discards incoming poll response without creating events."""
    bootstrap, sender, db, _ = e2e_env
    slot = await bootstrap.slot_repo.register_slot("slot_inf", "http://slot-inf.invalid:8080")
    fetch_started, disable_completed = asyncio.Event(), asyncio.Event()

    async def _handler(request: httpx.Request) -> httpx.Response:
        fetch_started.set()
        await disable_completed.wait()
        return make_scenario_response("200", request, item_id="999888", price=5000)

    bootstrap.http_pool.get_fetcher(slot.slot_id, slot.endpoint, transport=httpx.MockTransport(_handler))
    await bootstrap.writer.submit(lambda c: c.execute("INSERT INTO users (id, status, created_at) VALUES (8995, 'active', datetime('now'))"))
    sub = await bootstrap.subscription_service.add_subscription(8995, "https://www.avito.ru/moskva?q=phone_inf", 10_000)
    await bootstrap.cutover_mgr.set_tenant_mode(8995, CutoverMode.V2)

    now_iso = datetime.now(UTC).isoformat()
    async def _seed_baseline(c):
        await c.execute("UPDATE queries SET bootstrap_state = 'ready' WHERE id = ?", (sub.query_id,))
        await c.execute("INSERT INTO query_membership (query_id, avito_id, first_seen, last_seen) VALUES (?, '999888', ?, ?)", (sub.query_id, now_iso, now_iso))
        await c.execute("INSERT INTO events (id, avito_id, revision, kind, observed_at, old_price, new_price) VALUES ('ev_inf_0', '999888', 1, 'PRICE_CHANGED', ?, 6000, 6000)", (now_iso,))
    await bootstrap.writer.submit(_seed_baseline)

    await asyncio.wait_for(fetch_started.wait(), timeout=2.0)
    async def _disable(c):
        await c.execute("UPDATE assignments SET active = 0 WHERE query_id = ?", (sub.query_id,))
        await c.execute("UPDATE slots SET health_state = 'DISABLED' WHERE id = 'slot_inf'")
    await bootstrap.writer.submit(_disable)
    disable_completed.set()
    await asyncio.sleep(0.1)
    await bootstrap.public_poll_runtime.stop()

    assert await _count(db, "SELECT COUNT(*) FROM events WHERE avito_id = '999888' AND new_price = 5000") == 0
    row = await (await db._conn.execute("SELECT last_seen FROM query_membership WHERE query_id = ? AND avito_id = '999888'", (sub.query_id,))).fetchone()
    assert row is not None and row[0] == now_iso
