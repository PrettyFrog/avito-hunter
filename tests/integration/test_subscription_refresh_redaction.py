"""Worker refresh diagnostics must not expose callback exception payloads."""
import logging
from unittest.mock import AsyncMock

import pytest
from tests.integration.test_public_hunter import public_v2_system as public_v2_system

from hunter.application.public_query_edit import finish_query_edit
from hunter.application.subscriptions import change_subscription_price
from hunter.storage.public_edit_dialog import begin_edit
from hunter.storage.subscriptions import SubscriptionActor


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dialog", "price"])
async def test_refresh_failure_reports_saved_change_without_secret(
    public_v2_system, monkeypatch, caplog, kind
):
    app, _, _, _ = public_v2_system
    await app.dispatcher.stop()
    secret = "fake-refresh-password-value"
    service, actor = app.subscription_service, SubscriptionActor(10)
    monkeypatch.setattr(service, "on_query_assigned", AsyncMock(side_effect=OSError(secret)))
    with caplog.at_level(logging.ERROR):
        if kind == "dialog":
            await begin_edit(app.subscription_repo, actor, 100, "sub_u10", 1, "price")
            sub, refreshed = await finish_query_edit(service, actor, 100, "120000")
        else:
            sub, refreshed = await change_subscription_price(service, actor, "sub_u10", 1, 120000)
    assert sub.max_price == 120000 and not refreshed
    assert "refresh" in caplog.text.lower()
    assert secret not in caplog.text
