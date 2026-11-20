"""A price edit must change physical coverage, not just a local filter."""
import pytest

from hunter.core.models import User
from hunter.notify.telegram_handlers.public_status import edit_subscription
from hunter.storage.subscriptions import SubscriptionActor

pytest_plugins = ("tests.integration.test_public_hunter",)


@pytest.mark.asyncio
async def test_increase_ceiling_rebinds_only_editing_tenant(public_v2_system):
    app, _, old_qid, _ = public_v2_system
    await app.dispatcher.stop()
    user = User(id=10, telegram_user_id=10, chat_id=10, role="user", created_at="")
    ok, _ = await edit_subscription(user, "sub_u10", 120000, None, app.db)
    assert ok
    changed = await app.subscription_service.repo.get(SubscriptionActor(10), "sub_u10")
    other = await app.subscription_service.repo.get(SubscriptionActor(20), "sub_u20")
    assert changed.max_price == 120000 and changed.query_id != old_qid
    assert other.max_price == 100000 and other.query_id == old_qid
    row = await (await app.db._conn.execute(
        "SELECT canonical_url FROM queries WHERE id = ?", (changed.query_id,)
    )).fetchone()
    assert "pmax=120000" in row[0]


@pytest.mark.asyncio
async def test_refresh_failure_reports_saved_but_degraded(public_v2_system, monkeypatch):
    from unittest.mock import AsyncMock
    app, _, old_qid, _ = public_v2_system
    await app.dispatcher.stop()
    monkeypatch.setattr(
        app.subscription_service, "on_query_assigned", AsyncMock(side_effect=OSError)
    )
    user = User(id=10, telegram_user_id=10, chat_id=10, role="user", created_at="")
    ok, message = await edit_subscription(user, "sub_u10", 120000, "битый", app.db)
    changed = await app.subscription_service.repo.get(SubscriptionActor(10), "sub_u10")
    assert ok and "воркеры не удалось" in message
    assert changed.query_id != old_qid and changed.manual_stopwords == "битый"
