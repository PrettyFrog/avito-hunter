"""Exercise the public v2 CLI composition root with fake Telegram and HTTP."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Chat, Message, User as TelegramUser

from hunter.application.cutover import CutoverMode
from hunter.bootstrap import Bootstrap
from hunter.cli_parts.app_01 import check_public_v2_and_run
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.notify.telegram_router import setup_telegram_router


def _message(chat_id: int, text: str, bot: AsyncMock) -> Message:
    user = TelegramUser(id=chat_id, is_bot=False, first_name="CLI user")
    msg = Message(
        message_id=1, date=int(datetime.now(UTC).timestamp()),
        chat=Chat(id=chat_id, type="private"), from_user=user, text=text,
    )
    msg.as_(bot)
    object.__setattr__(msg, "reply", AsyncMock(return_value=True))
    return msg


class FakeTelegramChannel:
    def __init__(self, settings: Settings, bootstrap_ref: dict) -> None:
        self.settings = settings
        self.bootstrap_ref = bootstrap_ref
        self.chat_id = settings.telegram_chat_id
        self.sent: list[tuple[str, str]] = []
        self.delivered = asyncio.Event()
        self.stop_event: asyncio.Event | None = None
        self._bot = self

    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.chat_id)

    async def _client(self):
        return self._bot

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((str(chat_id), text))
        self.delivered.set()
        return type("SentMessage", (), {"message_id": 44})()

    async def listen(self, db=None, subscription_service=None) -> None:
        bootstrap = self.bootstrap_ref["bootstrap"]
        await db.create_user(
            telegram_user_id=7301, chat_id=7301, role="user", status="active"
        )
        await _install_fake_poll(bootstrap)
        await _run_public_wizard(self, db, subscription_service)
        await asyncio.wait_for(self.delivered.wait(), timeout=3)
        if self.stop_event is not None:
            self.stop_event.set()
        await asyncio.Future()


async def _install_fake_poll(bootstrap: Bootstrap) -> None:
    slot = await bootstrap.slot_repo.register_slot("cli_slot", "http://cli-slot:8080")
    fixture = Path("tests/fixtures/html/search_sample.html").read_text(encoding="utf-8")
    bodies, calls = [fixture, fixture.replace("9 900", "8 900")], 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        body = bodies[min(calls, len(bodies) - 1)]
        calls += 1
        return httpx.Response(200, text=body, request=request)

    bootstrap.http_pool.get_fetcher(
        slot.slot_id, slot.endpoint, transport=httpx.MockTransport(respond)
    )


async def _run_public_wizard(channel, db, service) -> None:
    bot = AsyncMock()
    router = setup_telegram_router(channel, db=db, subscription_service=service)
    texts = ("/start", "/add", "Телефон", "https://www.avito.ru/moskva?q=phone", "10000", "-")
    for text in texts:
        await router.propagate_event(
            "message", _message(7301, text, bot), db=db, bot=bot
        )
        if text == "/start":
            state = await db.cutover_manager.get_tenant_state(1)
            assert state.mode == CutoverMode.V2, "fresh public tenant must select V2 on /start"
    assert await service.list_subscriptions(1)


def _assert_cli_outbox(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "cli-v2.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM deliveries ORDER BY id LIMIT 1"
        ).fetchone()[0] == "sent"


def _new_cli_runtime(tmp_path: Path, settings: Settings, ref: dict) -> Bootstrap:
    bootstrap = Bootstrap(
        settings,
        db=Database(tmp_path / "cli-v2.db"),
        senders={"telegram": AsyncMock()},
    )
    ref["bootstrap"] = bootstrap
    return bootstrap


def _install_llm_fail_on_call(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from hunter.llm import router as router_module
    from hunter.llm.router import LLMRouter

    calls: list[str] = []

    def forbidden(name: str):
        async def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"Public v2 invoked LLM adapter: {name}")
        return fail

    for method in ("_gemini", "_gigachat", "_ollama", "_openrouter",
                   "_gemini_vision", "_gigachat_vision"):
        monkeypatch.setattr(LLMRouter, method, forbidden(method))
    for adapter in ("execute_gemini_call", "execute_gemini_vision_flow",
                    "execute_gigachat_vision", "execute_openrouter_vision",
                    "execute_ollama_vision", "execute_vision_dual"):
        monkeypatch.setattr(router_module, adapter, forbidden(adapter))
    return calls


@pytest.mark.asyncio
async def test_llm_fail_on_call_spy_detects_adapter_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from hunter.llm.router import LLMRouter

    calls = _install_llm_fail_on_call(monkeypatch)
    with pytest.raises(AssertionError, match="Public v2 invoked LLM adapter"):
        await LLMRouter()._gemini("prompt", system="")
    assert calls == ["_gemini"]


@pytest.mark.asyncio
async def test_cli_public_v2_bot_to_worker_http_parser_outbox_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_calls = _install_llm_fail_on_call(monkeypatch)
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.day_interval_sec = 0.05
    settings.public_v2.night_interval_sec = 0.1
    settings.telegram_bot_token = "fake-token"
    settings.telegram_chat_id = "9999"
    ref: dict = {}
    channel = FakeTelegramChannel(settings, ref)
    stop_event = asyncio.Event()
    channel.stop_event = stop_event

    assert await check_public_v2_and_run(
        settings,
        channel_factory=lambda _settings: channel,
        bootstrap_factory=lambda _settings: _new_cli_runtime(tmp_path, settings, ref),
        stop_event=stop_event,
    )
    assert channel.sent and channel.sent[0][0] == "7301"
    assert "8900" in channel.sent[0][1] or "8 900" in channel.sent[0][1]
    _assert_cli_outbox(tmp_path)
    assert llm_calls == []
