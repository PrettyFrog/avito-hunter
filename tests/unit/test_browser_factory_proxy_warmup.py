from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.proxy_credentials import ProxyCredentials


def _slot(endpoint: str = "http://proxy.example:8080") -> SlotSnapshot:
    return SlotSnapshot(
        slot_id="slot_1",
        endpoint=endpoint,
        role=SlotRole.ACTIVE,
        health=SlotHealth.READY,
        session_version=1,
        generation=1,
        updated_at="2026-11-13",
    )


@pytest.mark.asyncio
async def test_browser_proxy_without_credentials_and_direct():
    from hunter.transport.browser_factory import _browser_proxy

    assert await _browser_proxy("http://proxy.example:8080") == (
        {"server": "http://proxy.example:8080"},
        None,
    )
    assert await _browser_proxy("direct") == (None, None)
    assert await _browser_proxy("") == (None, None)


@pytest.mark.asyncio
async def test_browser_proxy_with_credentials_uses_loopback_relay():
    from hunter.transport.browser_factory import _browser_proxy

    proxy, relay = await _browser_proxy(
        "http://proxy.example:8080", ProxyCredentials("operator", "secret")
    )
    try:
        assert relay is not None
        assert proxy is not None
        assert proxy["server"].startswith("http://127.0.0.1:")
        assert "username" not in proxy
        assert "password" not in proxy
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_bind_factory_stores_respects_protected_proxy_flag():
    from types import SimpleNamespace

    from hunter.transport.factory_bindings import bind_factory_stores

    sessions: list[object] = []
    credentials: list[object] = []
    factory = SimpleNamespace(
        bind_session_store=lambda store: sessions.append(store),
        bind_credential_store=lambda store: credentials.append(store),
    )

    await bind_factory_stores(factory, "sessions", "creds", False)
    await bind_factory_stores(factory, "sessions", "creds", True)

    assert sessions == ["sessions", "sessions"]
    assert credentials == [None, "creds"]


@pytest.mark.asyncio
async def test_warmup_needed_without_ft_on_ip_block_page():
    from hunter.transport.browser_factory import _needs_fingerprint_warmup

    context = AsyncMock()
    context.cookies = AsyncMock(return_value=[{"name": "_adcc", "value": "1"}])
    page = AsyncMock()
    page.title = AsyncMock(return_value="Доступ ограничен: проблема с IP")

    assert await _needs_fingerprint_warmup(context, page) is True


@pytest.mark.asyncio
async def test_warmup_not_needed_when_ft_present():
    from hunter.transport.browser_factory import _needs_fingerprint_warmup

    context = AsyncMock()
    context.cookies = AsyncMock(return_value=[{"name": "ft", "value": "x"}])
    page = AsyncMock()
    page.title = AsyncMock(return_value="Доступ ограничен: проблема с IP")

    assert await _needs_fingerprint_warmup(context, page) is False


@pytest.mark.asyncio
async def test_warmup_not_needed_on_healthy_page_without_ft():
    from hunter.transport.browser_factory import _needs_fingerprint_warmup

    context = AsyncMock()
    context.cookies = AsyncMock(return_value=[])
    page = AsyncMock()
    page.title = AsyncMock(return_value="Авито: недвижимость, транспорт, работа, услуги, вещи")

    assert await _needs_fingerprint_warmup(context, page) is False


@pytest.mark.asyncio
async def test_warmup_navigates_to_technical_page_until_ft():
    from hunter.transport.browser_factory import _warmup_fingerprint

    page = AsyncMock()
    page.goto = AsyncMock()
    page.context.cookies = AsyncMock(side_effect=[
        [{"name": "f", "value": "1"}],
        [{"name": "ft", "value": "token"}],
    ])

    solved = await _warmup_fingerprint(page, "https://www.avito.ru", timeout_sec=2.0)

    assert solved is True
    page.goto.assert_awaited_once()
    assert page.goto.await_args.args[0] in ("https://www.avito.ru/", "https://www.avito.ru/info/show_technical")


@pytest.mark.asyncio
async def test_factory_loads_slot_proxy_credentials(monkeypatch):
    from hunter.transport import browser_factory

    captured: dict[str, object] = {}

    async def fake_open(slot, settings, session_store, credentials=None):
        captured["credentials"] = credentials
        return object()

    monkeypatch.setattr(browser_factory, "_open_slot_page", fake_open)
    store = AsyncMock()
    store.load = lambda slot_id, endpoint: ProxyCredentials("operator", "secret")
    factory = browser_factory.create_slot_browser_factory(object(), credential_store=store)

    await factory(_slot())

    assert captured["credentials"] == ProxyCredentials("operator", "secret")


@pytest.mark.asyncio
async def test_camoufox_failure_keeps_relay_for_chromium_fallback(monkeypatch):
    from types import SimpleNamespace

    from hunter.transport import browser_factory

    relay = SimpleNamespace(stop=AsyncMock())
    fallback = (object(), object(), AsyncMock())

    async def fake_proxy(endpoint, credentials=None):
        return {"server": "http://127.0.0.1:1"}, relay

    async def fake_camoufox(*args, **kwargs):
        raise RuntimeError("camoufox unavailable")

    async def fake_chromium(*args, **kwargs):
        return fallback

    monkeypatch.setattr(browser_factory, "_browser_proxy", fake_proxy)
    monkeypatch.setattr(browser_factory, "_launch_camoufox", fake_camoufox)
    monkeypatch.setattr(browser_factory, "_launch_chromium", fake_chromium)

    assert await browser_factory._launch_browser_context(_slot()) is fallback
    relay.stop.assert_not_awaited()

