"""Public CLI starts an enabled VK listener and preflights its credentials."""

from __future__ import annotations

import asyncio

import pytest

from hunter.cli_parts.app_01 import check_public_v2_and_run
from hunter.core.config import Settings


class _Channel:
    chat_id = "999"

    def __init__(self, _settings: Settings) -> None:
        pass

    def enabled(self) -> bool:
        return True

    async def listen(self, db=None, subscription_service=None) -> None:
        return None


class _Bootstrap:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.senders: dict[str, object] = {}
        self._tasks: list[asyncio.Task] = []
        self.db = object()
        self.writer = object()
        self.subscription_service = object()
        self.shutdown_called = False

    async def startup(self) -> None:
        assert callable(self.senders["telegram"])

    async def run_supervised(self, _stop_event: asyncio.Event) -> int:
        await asyncio.sleep(0)
        return 0

    async def shutdown(self) -> None:
        self.shutdown_called = True
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class _Listener:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False

    async def run(self, _stop_event: asyncio.Event) -> None:
        self.started = True
        await self.kwargs["client"].close()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        public_v2={"enabled": True},
        telegram_bot_token="fake-telegram-token",
        telegram_chat_id="999",
        vk_binding_enabled=True,
        vk_group_id=42,
        vk_group_token="fake-vk-token",
        channel_binding_secret="private-binding-secret-1234567890",
    )


@pytest.mark.asyncio
async def test_cli_wires_vk_listener_only_after_bootstrap_startup() -> None:
    settings = _settings()
    bootstrap = _Bootstrap(settings)
    made: list[_Listener] = []

    def listener_factory(**kwargs) -> _Listener:
        listener = _Listener(**kwargs)
        made.append(listener)
        return listener

    assert await check_public_v2_and_run(
        settings,
        channel_factory=_Channel,
        bootstrap_factory=lambda _settings: bootstrap,
        vk_listener_factory=listener_factory,
        stop_event=asyncio.Event(),
    )
    assert len(made) == 1
    assert made[0].started
    assert made[0].kwargs["group_id"] == 42
    assert made[0].kwargs["writer"] is bootstrap.writer
    assert callable(bootstrap.senders["vk"])
    assert bootstrap.shutdown_called


@pytest.mark.asyncio
async def test_cli_rejects_incomplete_vk_config_before_bootstrap() -> None:
    settings = Settings(_env_file=None, public_v2={"enabled": True})
    settings.vk_binding_enabled = True
    boot_called = False

    def unexpected_bootstrap(_settings: Settings) -> _Bootstrap:
        nonlocal boot_called
        boot_called = True
        raise AssertionError("Bootstrap must not start with incomplete VK settings")

    with pytest.raises(RuntimeError, match="group ID, group token, and binding secret"):
        await check_public_v2_and_run(
            settings,
            channel_factory=_Channel,
            bootstrap_factory=unexpected_bootstrap,
            stop_event=asyncio.Event(),
        )
    assert not boot_called
