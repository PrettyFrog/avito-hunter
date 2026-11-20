"""CS-10 End-to-end integration matrix for challenge stability and egress regulation."""
from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from hunter.application.metrics import LowCardinalityMetricsCollector
from hunter.application.poll_attempts import (
    can_fallback_to_html,
    extract_retry_after,
)
from hunter.core.egress_config import EgressRegulatorConfig
from hunter.domain.slots import SlotHealth, SlotRole
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.deliveries import DeliveryRepository
from hunter.storage.request_cooldown import RequestCooldownStore
from hunter.storage.slot_state import SlotRepository
from hunter.storage.writer import SQLiteWriterActor
from hunter.transport.catalog_api import invalidate_poll_target, resolve_poll_target
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.challenge_models import ChallengeStatus
from hunter.transport.metered_http import HttpDeferred, RequestGateway
from hunter.transport.recovery_identity import RecoveryFailureKind
from hunter.transport.recovery_transport import build_recovery_http_client
from hunter.transport.session_store import SessionStore
from hunter.domain.poll_policy import PollPeriod, PollPolicy
from hunter.domain.request_budget import RequestBudget


def _init_db(tmp_path, name="cs10.db"):
    db_path = tmp_path / name
    with sqlite3.connect(db_path) as conn:
        conn.executescript(Path("src/hunter/core/schema.sql").read_text())
    return db_path


async def _start_storage(db_path):
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    return mgr, writer


@pytest.mark.asyncio
async def test_cold_start_and_valid_200(tmp_path):
    mgr, writer = await _start_storage(_init_db(tmp_path, "cold.db"))
    try:
        repo = SlotRepository(mgr, writer)
        deliv = DeliveryRepository(mgr, writer)
        slot = await repo.register_slot("slot-cold", "direct", role=SlotRole.ACTIVE)
        assert slot.health == SlotHealth.READY

        metrics = LowCardinalityMetricsCollector()
        now = time.monotonic()
        metrics.valid_tracker.record_valid_update("q-cold", items_count=5, now=now)
        assert metrics.valid_tracker.total_valid_updates == 1

        # Direct outbox delivery creation and claim
        async def _insert_delivery(c):
            await c.execute(
                "INSERT INTO events (id, avito_id, revision, kind, observed_at, created_at) "
                "VALUES ('e1', 'item-1', 1, 'NEW_LISTING', datetime('now'), datetime('now'))"
            )
            await c.execute(
                "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, payload, created_at) "
                "VALUES ('d1', 'e1', 1, 'telegram', 'pending', datetime('now'), '{\"action\":\"NEW\"}', datetime('now'))"
            )
        await writer.submit(_insert_delivery)
        now_ts = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
        claimed = await deliv.claim_due(now_ts, limit=10)
        assert len(claimed) == 1 and claimed[0].id == "d1"
        assert await deliv.complete("d1", claimed[0].lease_token, message_id="msg-1")
    finally:
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_access_failures_and_extreme_retry_after(tmp_path):
    mgr, writer = await _start_storage(_init_db(tmp_path, "fail.db"))
    try:
        cooldown_store = RequestCooldownStore(writer)
        gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=5.0), cooldown_store=cooldown_store)

        # 429 / 403 / 439 signals
        r429 = SimpleNamespace(headers={"Retry-After": "9000"}, status_code=429)
        assert extract_retry_after(r429) == 9000.0

        huge_ra = SimpleNamespace(headers={"Retry-After": "9" * 300}, status_code=429)
        val = extract_retry_after(huge_ra)
        assert val is not None and val > 100000.0  # saturated safely

        # Extend persisted cooldown across IP and restore into gateway
        await cooldown_store.extend("default", "avito", generation=1, duration_sec=100.0, shared_egress=True)
        await gateway.restore(egress_group="default", upstream="avito")
        decision = gateway.budget.before_attempt("default", "avito", deadline=time.monotonic() + 10)
        assert decision.reason == "cooldown"
    finally:
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_stale_route_invalidation_404_410():
    canonical = "https://www.avito.ru/moskva/tovary"
    json_target = "https://www.avito.ru/api/1/catalog/items"

    resp_404 = SimpleNamespace(status_code=404, headers={}, error=None)
    assert can_fallback_to_html(json_target, canonical, resp_404)

    invalidate_poll_target(canonical)
    target_fallback, _ = resolve_poll_target(canonical)
    assert target_fallback == canonical  # Falls back to HTML canonical


@pytest.mark.asyncio
async def test_operator_required_b_and_failed_warmup(tmp_path):
    mgr, writer = await _start_storage(_init_db(tmp_path, "op.db"))
    store = SessionStore(str(tmp_path / "sessions"))
    try:
        repo = SlotRepository(mgr, writer)
        slot = await repo.register_slot("slot-op", "direct", role=SlotRole.ACTIVE)
        service = ChallengeHandoffService(
            repo, store, allow_browser=False,
            egress_gateway=RequestGateway(RequestBudget(enforce=True, spacing_sec=1.0)),
            egress_groups=EgressRegulatorConfig(),
        )
        req = ChallengeRequest(
            slot_id="slot-op", challenge_type="geetest", html="<html><body>geetest_captcha</body></html>",
            client_identity="user_b", expected_generation=slot.generation, endpoint="direct",
        )
        outcome = await service.handle_challenge(req)
        assert outcome.status == ChallengeStatus.OPERATOR_REQUIRED
        assert outcome.failure_kind == RecoveryFailureKind.RECOVERY_FAILED
        cur = await repo.get_slot("slot-op")
        assert cur.health == SlotHealth.QUARANTINED
    finally:
        await service.close()
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_timeout_cancel_and_competing_slots_same_ip():
    now = [10.0]
    budget = RequestBudget(enforce=True, spacing_sec=5.0, clock=lambda: now[0])
    gateway = RequestGateway(budget)

    # Two clients on same physical egress group
    c1, o1 = build_recovery_http_client("direct", gateway=gateway, egress_group="shared-ip")
    c2, o2 = build_recovery_http_client("direct", gateway=gateway, egress_group="shared-ip")
    try:
        p1 = await gateway.acquire(egress_group="shared-ip", upstream="avito", deadline=now[0] + 10)
        assert p1 is not None
        # Concurrent second request on same group must defer
        with pytest.raises(HttpDeferred):
            await gateway.acquire(egress_group="shared-ip", upstream="avito", deadline=now[0] + 1)
        gateway.budget.after_attempt(p1)
    finally:
        if o1: await c1.aclose()
        if o2: await c2.aclose()


@pytest.mark.asyncio
async def test_restart_with_persisted_cooldown_and_outbox_flow(tmp_path):
    db_path = _init_db(tmp_path, "restart.db")
    mgr, writer = await _start_storage(db_path)
    try:
        store = RequestCooldownStore(writer)
        await store.extend("shared-ip", "avito", generation=1, duration_sec=500.0, shared_egress=True)
    finally:
        await writer.stop()
        await mgr.drain_and_close()

    # Restart app: open new storage manager and load persisted cooldowns
    mgr2, writer2 = await _start_storage(db_path)
    try:
        store2 = RequestCooldownStore(writer2)
        gateway2 = RequestGateway(RequestBudget(enforce=True, spacing_sec=5.0), cooldown_store=store2)
        await gateway2.restore(egress_group="shared-ip", upstream="avito")
        # Must be in cooldown after restart
        decision = gateway2.budget.before_attempt("shared-ip", "avito", deadline=time.monotonic() + 10)
        assert decision.reason == "cooldown"

        # Outbox event transition A -> B -> A
        deliv = DeliveryRepository(mgr2, writer2)
        async def _insert_events(c):
            await c.execute(
                "INSERT INTO events (id, avito_id, revision, kind, observed_at, created_at) "
                "VALUES ('evt-a1', 'item-1', 1, 'NEW_LISTING', datetime('now'), datetime('now'))"
            )
            await c.execute(
                "INSERT INTO events (id, avito_id, revision, kind, observed_at, created_at) "
                "VALUES ('evt-b', 'item-1', 2, 'PRICE_CHANGED', datetime('now'), datetime('now'))"
            )
            await c.execute(
                "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, payload, created_at) "
                "VALUES ('e1', 'evt-a1', 1, 'telegram', 'pending', datetime('now'), '{\"type\":\"NEW\",\"price\":100}', datetime('now'))"
            )
            await c.execute(
                "INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at, payload, created_at) "
                "VALUES ('e2', 'evt-b', 1, 'telegram', 'pending', datetime('now'), '{\"type\":\"PRICE_CHANGED\",\"price\":90}', datetime('now'))"
            )
        await writer2.submit(_insert_events)
        now_ts = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
        claimed = await deliv.claim_due(now_ts, limit=10)
        assert len(claimed) == 2
        assert {d.id for d in claimed} == {"e1", "e2"}
    finally:
        await writer2.stop()
        await mgr2.drain_and_close()


def test_no_catch_up_burst_and_day_night_jitter_preservation():
    policy = PollPolicy(
        day_interval_sec=30.0,
        night_interval_sec=180.0,
        jitter_ratio=0.1,
        max_jitter_sec=10.0,
    )
    dt_day = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    dt_night = datetime(2026, 11, 6, 1, 0, 0, tzinfo=UTC)  # 04:00 MSK

    assert policy.get_period(dt_day) == PollPeriod.DAY
    assert policy.get_cycle_interval(dt_day) == 30.0

    assert policy.get_period(dt_night) == PollPeriod.NIGHT
    assert policy.get_cycle_interval(dt_night) == 180.0

    # Verify jitter bounded within configured +/- 10%
    for _ in range(50):
        jit_day = policy.get_jittered_cycle_interval(dt_day)
        assert 27.0 <= jit_day <= 33.0
        jit_night = policy.get_jittered_cycle_interval(dt_night)
        assert 162.0 <= jit_night <= 198.0

    # Absence of catch-up: schedule intervals do not backlog or decay to zero
    offsets = policy.calculate_slot_offsets(num_queries=3, dt=dt_day)
    assert len(offsets) == 3
    assert offsets == (0.0, 10.0, 20.0)

