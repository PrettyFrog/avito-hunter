"""Offline proof that a persisted assignment reaches HTTP, parser, SQLite, and outbox."""

from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from aiogram.types import Chat, Message, User as TelegramUser

from hunter.application.cutover import CutoverMode
from hunter.application.dispatcher import SendResult
from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.notify.public_telegram_sender import PublicTelegramSender
from hunter.notify.telegram_router import setup_telegram_router
from hunter.transport.http_fetch import SlotHttpPool


async def _count(db: Database, query: str) -> int:
    return int((await (await db._conn.execute(query)).fetchone())[0])


@pytest.mark.asyncio
async def test_slot_worker_polls_fake_http_and_persists_price_change(tmp_path: Path) -> None:
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.day_interval_sec = 0.05
    settings.public_v2.night_interval_sec = 0.1
    db = Database(tmp_path / "poll.db")
    sender = AsyncMock(return_value=SendResult("sent", "fake-message"))
    bootstrap = Bootstrap(settings, db=db, senders={"telegram": sender})
    await bootstrap.startup()
    try:
        await bootstrap.dispatcher.stop()
        slot = await bootstrap.slot_repo.register_slot("poll_slot", "http://slot.invalid:8080")
        fixture = Path("tests/fixtures/html/search_sample.html").read_text(encoding="utf-8")
        responses = [fixture, fixture.replace("9 900", "8 900")]
        calls = 0
        second_response = asyncio.Event()

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            body = responses[min(calls, len(responses) - 1)]
            calls += 1
            if calls >= 2:
                second_response.set()
            return httpx.Response(200, text=body, request=request)

        bootstrap.http_pool.get_fetcher(
            slot.slot_id,
            slot.endpoint,
            transport=httpx.MockTransport(respond),
        )
        await bootstrap.writer.submit(lambda conn: conn.execute(
            "INSERT INTO users (id, status, created_at) VALUES (7001, 'active', datetime('now'))"
        ))
        sub = await bootstrap.subscription_service.add_subscription(
            7001, "https://www.avito.ru/moskva?q=phone", 10_000
        )
        await bootstrap.cutover_mgr.set_tenant_mode(7001, CutoverMode.V2)
        assert bootstrap.workers is bootstrap.public_poll_runtime.workers
        worker = bootstrap.workers[slot.slot_id]
        assert worker.assigned_queries == [sub.query_id]
        assignment = bootstrap.public_poll_runtime.assignments[(slot.slot_id, sub.query_id)]
        row = await (
            await bootstrap.db._conn.execute(
                "SELECT generation FROM assignments WHERE slot_id = ? AND "
                "query_id = ? AND active = 1",
                (slot.slot_id, sub.query_id),
            )
        ).fetchone()
        assert assignment.generation == int(row[0])

        await asyncio.wait_for(second_response.wait(), timeout=2)
        for _ in range(100):
            if await _count(bootstrap.db, "SELECT COUNT(*) FROM events"):
                break
            await asyncio.sleep(0.01)
        await bootstrap.public_poll_runtime.stop()
        assert calls >= 2
        counts = [
            await _count(bootstrap.db, query)
            for query in (
                "SELECT COUNT(*) FROM events",
                "SELECT COUNT(*) FROM deliveries WHERE status = 'pending'",
                "SELECT COUNT(*) FROM listing_state WHERE avito_id = '1112223334'",
            )
        ]
        assert counts == [1, 1, 1]
        assert await bootstrap.dispatcher.dispatch_once() == 1
        sender.assert_awaited_once()
    finally:
        await bootstrap.shutdown()


def _private_message(chat_id: int, text: str, bot: AsyncMock) -> Message:
    user = TelegramUser(id=chat_id, is_bot=False, first_name="Public user")
    message = Message(
        message_id=1,
        date=int(datetime.now(UTC).timestamp()),
        chat=Chat(id=chat_id, type="private"),
        from_user=user,
        text=text,
    )
    message.as_(bot)
    object.__setattr__(message, "reply", AsyncMock(return_value=True))
    return message


async def _run_bot_add_to_delivery(tmp_path: Path) -> tuple[list, int]:
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.day_interval_sec = 0.05
    settings.public_v2.night_interval_sec = 0.1
    bootstrap = Bootstrap(settings, db=Database(tmp_path / "bot-runtime.db"))
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return type("SentMessage", (), {"message_id": 22})()

    class Channel:
        chat_id = "9999"

    bot, telegram_bot, channel = AsyncMock(), Bot(), Channel()
    channel._client = AsyncMock(return_value=telegram_bot)
    bootstrap.senders["telegram"] = PublicTelegramSender(bootstrap, channel)
    await bootstrap.startup()
    await bootstrap.dispatcher.stop()
    try:
        user_id = await bootstrap.db.create_user(
            telegram_user_id=7001, chat_id=7001, role="user", status="active"
        )
        await bootstrap.cutover_mgr.set_tenant_mode(user_id, CutoverMode.V2)
        slot = await bootstrap.slot_repo.register_slot("bot_slot", "http://bot-slot:8080")
        fixture = Path("tests/fixtures/html/search_sample.html").read_text(encoding="utf-8")
        bodies, calls, changed = [fixture, fixture.replace("9 900", "8 900")], 0, asyncio.Event()

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            body = bodies[min(calls, len(bodies) - 1)]
            calls += 1
            if calls >= 2:
                changed.set()
            return httpx.Response(200, text=body, request=request)

        bootstrap.http_pool.get_fetcher(
            slot.slot_id, slot.endpoint, transport=httpx.MockTransport(respond)
        )
        router = setup_telegram_router(
            channel, db=bootstrap.db, subscription_service=bootstrap.subscription_service
        )
        texts = ("/start", "/add", "Телефон", "https://www.avito.ru/moskva?q=phone", "10000", "-")
        for text in texts:
            await router.propagate_event(
                "message", _private_message(7001, text, bot), db=bootstrap.db, bot=bot
            )
        assert await bootstrap.subscription_service.list_subscriptions(user_id)
        assignments = await (
            await bootstrap.db._conn.execute(
                "SELECT slot_id, query_id FROM assignments WHERE active = 1"
            )
        ).fetchall()
        assert assignments, "bot add did not persist a public slot assignment"
        assert bootstrap.workers, f"assignment did not start a worker: {assignments!r}"
        await asyncio.wait_for(changed.wait(), timeout=2)
        await _await_delivery(bootstrap, sent)
        return sent, calls
    finally:
        await bootstrap.shutdown()


async def _await_delivery(bootstrap, sent: list):
    for _ in range(100):
        pending = await _count(
            bootstrap.db, "SELECT COUNT(*) FROM deliveries WHERE status = 'pending'"
        )
        if pending:
            break
        await asyncio.sleep(0.01)
    assert await bootstrap.dispatcher.dispatch_once() == 1
    assert sent and sent[0][0] == "7001" and ("8900" in sent[0][1] or "8 900" in sent[0][1])
    assert await bootstrap.db.get_pending_product(7001) is None


@pytest.mark.asyncio
async def test_cli_bot_add_runs_worker_and_public_telegram_delivery(tmp_path: Path) -> None:
    sent, calls = await _run_bot_add_to_delivery(tmp_path)
    assert calls >= 2
    assert sent[0][0] == "7001"


async def _install_restart_pool(
    slot_id: str, endpoint: str, responses: list[str]
) -> tuple[SlotHttpPool, dict]:
    pool = SlotHttpPool()
    calls = {"count": 0}

    async def respond(request: httpx.Request) -> httpx.Response:
        index = min(calls["count"], len(responses) - 1)
        calls["count"] += 1
        return httpx.Response(200, text=responses[index], request=request)

    pool.get_fetcher(slot_id, endpoint, transport=httpx.MockTransport(respond))
    return pool, calls


@pytest.mark.asyncio
async def test_bootstrap_restart_restores_persisted_assignment_and_polls(tmp_path: Path) -> None:
    db_path = tmp_path / "restart.db"
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.day_interval_sec = 0.05
    settings.public_v2.night_interval_sec = 0.1
    fixture = Path("tests/fixtures/html/search_sample.html").read_text(encoding="utf-8")
    first_pool, _ = await _install_restart_pool("active_01", "http://slot.invalid:8080", [""])
    first = Bootstrap(
        settings, db=Database(db_path), senders={"telegram": AsyncMock()}, http_pool=first_pool
    )
    await first.startup()
    user_id = await first.db.create_user(
        telegram_user_id=7002, chat_id=7002, role="user", status="active"
    )
    await first.slot_repo.register_slot("active_01", "http://slot.invalid:8080")
    await first.subscription_service.add_subscription(
        user_id, "https://www.avito.ru/moskva?q=phone", 10_000
    )
    await first.cutover_mgr.set_tenant_mode(user_id, CutoverMode.V2)
    await first.shutdown()

    changed = fixture.replace("9 900", "8 900")
    second_pool, calls = await _install_restart_pool(
        "active_01", "http://slot.invalid:8080", [fixture, changed]
    )
    sender = AsyncMock(return_value=SendResult("sent", "restart-message"))
    second = Bootstrap(
        settings, db=Database(db_path), senders={"telegram": sender}, http_pool=second_pool
    )
    await second.startup()
    try:
        worker = second.workers["active_01"]
        assert len(worker.assigned_queries) == 1
        for _ in range(200):
            if sender.await_count:
                break
            await asyncio.sleep(0.01)
        assert calls["count"] >= 2
        sender.assert_awaited()
        assert await _count(second.db, "SELECT COUNT(*) FROM events") >= 1
    finally:
        await second.shutdown()
