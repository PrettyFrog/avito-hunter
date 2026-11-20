"""End-to-end integration tests for T-B091: Full user lifecycle scenario (RB-2).

Verifies the complete real user flow:
1. Create user and register initial search subscription with slot allocation.
2. Edit search parameters (raise price ceiling, add stop-words, update name).
3. First poll executes quiet baseline (0 notifications dispatched).
4. Second poll with eligible listing triggers outbox delivery generation.
5. Stop-word filters correctly suppress matching defective listings.
6. Dispatcher delivers pending notifications and marks them 'sent'.
7. Service restarts cleanly: subscriptions, delivery history, and baseline persist without duplicate alerts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import aiosqlite
import pytest

from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.application.public_query_edit import finish_query_edit
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.core.models import ObservedListing, PriceState
from hunter.domain.observations import ObservationBatch, ParseOutcome
from hunter.storage.observation_tx import execute_observation_tx
from hunter.storage.public_edit_dialog import begin_edit
from hunter.storage.subscriptions import SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


async def _init_user_and_sub(app: Bootstrap, user_id: int, chat_id: int, actor: SubscriptionActor) -> str:
    async def init_slot_and_user(conn: aiosqlite.Connection):
        await conn.execute(
            "INSERT INTO users (id, telegram_user_id, chat_id, role, status, created_at) "
            "VALUES (?, ?, ?, 'user', 'active', datetime('now'))",
            (user_id, user_id, chat_id),
        )
        await conn.execute(
            "INSERT INTO slots (id, endpoint, cookie_namespace, health_state, active_generation) "
            "VALUES ('slot_primary', 'http://127.0.0.1:8000', 'default', 'healthy', 1)"
        )

    await app.writer.submit(init_slot_and_user)
    await app.cutover_mgr.set_tenant_mode(user_id, CutoverMode.V2)

    initial_url = "https://www.avito.ru/moskva/noutbuki?pmax=80000&s=104"
    sub_record = await app.subscription_service.add_subscription(
        user_id, initial_url, 80000, sub_id="sub_e2e", name="Ноутбуки"
    )
    assert sub_record.id == "sub_e2e" and sub_record.version == 1

    await begin_edit(app.subscription_repo, actor, chat_id, "sub_e2e", 1, "price")
    sub_edited, refreshed = await finish_query_edit(
        app.subscription_service, actor, chat_id, "90 000 руб"
    )
    assert refreshed is True and sub_edited.max_price == 90000 and sub_edited.version == 2
    rebound_qid = sub_edited.query_id

    await app.subscription_repo.update(actor, "sub_e2e", expected_version=2, words="битый,запчасти")
    sub_final = await app.subscription_repo.get(actor, "sub_e2e")
    assert sub_final.version == 3 and sub_final.manual_stopwords == "битый,запчасти"
    return rebound_qid


async def _run_baseline_and_fresh_poll(app: Bootstrap, rebound_qid: str) -> None:
    batch_baseline = ObservationBatch(
        slot_id="slot_primary",
        query_id=rebound_qid,
        observed_at="2026-10-30T10:00:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing("lot_old_1", "ThinkPad T14", 75000, PriceState.ACTIVE, "https://avito.ru/old1"),
            ObservedListing("lot_old_2", "MacBook Air M1", 85000, PriceState.ACTIVE, "https://avito.ru/old2"),
        ),
    )
    res_b1 = await app.writer.submit(lambda c: execute_observation_tx(c, batch_baseline, request_sequence=1))
    assert res_b1.is_bootstrap is True and res_b1.deliveries_created == 0

    batch_fresh = ObservationBatch(
        slot_id="slot_primary",
        query_id=rebound_qid,
        observed_at="2026-10-30T10:05:00+00:00",
        outcome=ParseOutcome.OK,
        listings=(
            ObservedListing("lot_old_1", "ThinkPad T14", 75000, PriceState.ACTIVE, "https://avito.ru/old1"),
            ObservedListing("lot_old_2", "MacBook Air M1", 85000, PriceState.ACTIVE, "https://avito.ru/old2"),
            ObservedListing("lot_deal_fresh", "MacBook Air M2 16GB", 88000, PriceState.ACTIVE, "https://avito.ru/deal"),
            ObservedListing("lot_defect", "MacBook Air битый дисплей", 40000, PriceState.ACTIVE, "https://avito.ru/defect"),
        ),
    )
    res_b2 = await app.writer.submit(lambda c: execute_observation_tx(c, batch_fresh, request_sequence=2))
    assert res_b2.is_bootstrap is False and res_b2.deliveries_created == 1


async def _dispatch_and_verify(app: Bootstrap, user_id: int, sent_alerts: list[dict]) -> None:
    async def make_ready(conn: aiosqlite.Connection) -> None:
        await conn.execute("UPDATE deliveries SET available_at = '2020-01-01T00:00:00+00:00' WHERE status='pending'")

    await app.dispatcher.stop()
    await app.writer.submit(make_ready)
    d_count = await app.dispatcher.dispatch_once("2026-10-30T10:06:00+00:00")
    assert d_count == 1
    assert len(sent_alerts) == 1 and sent_alerts[0]["user_id"] == user_id


async def _verify_service_restart(
    settings: Settings, db_path: Path, actor: SubscriptionActor, mock_tg_sender: Any, sent_alerts: list[dict]
) -> None:
    restarted_app = Bootstrap(settings, db=Database(db_path), senders={"telegram": mock_tg_sender})
    await restarted_app.startup()
    await restarted_app.dispatcher.stop()
    try:
        restarted_sub = await restarted_app.subscription_repo.get(actor, "sub_e2e")
        assert restarted_sub.max_price == 90000 and restarted_sub.version == 3
        assert restarted_sub.manual_stopwords == "битый,запчасти"

        await restarted_app.dispatcher.dispatch_once("2026-10-30T10:10:00+00:00")
        assert len(sent_alerts) == 1

        cur = await restarted_app.db._conn.execute("SELECT status, message_id FROM deliveries")
        deliveries = await cur.fetchall()
        assert len(deliveries) == 1 and deliveries[0][0] == "sent" and deliveries[0][1] == "msg_1"
    finally:
        await restarted_app.shutdown()


@pytest.mark.asyncio
async def test_full_user_lifecycle_scenario_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNTER_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "lifecycle_e2e.db"
    settings = Settings(env="test")
    settings.public_v2.enabled = True

    sent_alerts: list[dict] = []

    async def mock_tg_sender(delivery) -> SendResult:
        sent_alerts.append({"user_id": delivery.user_id, "id": delivery.id})
        return SendResult("sent", f"msg_{len(sent_alerts)}")

    app = Bootstrap(settings, db=Database(db_path), senders={"telegram": mock_tg_sender})
    await app.startup()

    user_id = 500
    chat_id = 500
    actor = SubscriptionActor(user_id)

    try:
        rebound_qid = await _init_user_and_sub(app, user_id, chat_id, actor)
        await _run_baseline_and_fresh_poll(app, rebound_qid)
        await _dispatch_and_verify(app, user_id, sent_alerts)
    finally:
        await app.shutdown()

    await _verify_service_restart(settings, db_path, actor, mock_tg_sender, sent_alerts)
