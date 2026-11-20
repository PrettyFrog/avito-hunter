"""End-to-end loopback verification that all public HTTP paths route through RequestGateway."""
import time
from unittest.mock import Mock

import pytest
from aiohttp import web

from hunter.core.egress_config import EgressRegulatorConfig
from hunter.domain.request_budget import RequestBudget
from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig
from hunter.transport.metered_http import HttpDeferred, RequestGateway
from hunter.transport.price_fetch import PriceFetchAdapter, make_price_client
from hunter.transport.recovery_transport import build_recovery_http_client


@pytest.fixture
async def loopback_wire():
    wire = []
    async def handler(request):
        wire.append((request.path, request.method, time.monotonic()))
        return web.Response(text="<html><body><div data-marker='catalog'>item</div></body></html>")
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", wire
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_all_public_http_paths_enforce_gateway_and_count_wire(loopback_wire):
    base_url, wire = loopback_wire
    now = [100.0]
    spacing = 0.05
    budget = RequestBudget(enforce=True, spacing_sec=spacing, clock=lambda: now[0])
    gateway = RequestGateway(budget)

    # 1. Price check path
    price_client = make_price_client(gateway=gateway)
    price_fetcher = PriceFetchAdapter(price_client, clock=lambda: now[0])
    try:
        res1 = await price_fetcher.fetch_card_price(f"{base_url}/item/1")
        assert len(wire) == 1
        assert res1.status in ("ok", "unknown")
        # Immediate second call before spacing must defer without reaching wire
        res2 = await price_fetcher.fetch_card_price(f"{base_url}/item/2")
        assert res2.status == "deferred"
        assert len(wire) == 1
    finally:
        await price_fetcher.close()

    # Advance clock past spacing
    now[0] += spacing + 0.1

    # 2. Recovery HTTP client GET path
    rec_client, owns = build_recovery_http_client(
        "direct", gateway=gateway, egress_group="default"
    )
    try:
        resp = await rec_client.get(f"{base_url}/poll")
        assert resp.status_code == 200
        await resp.aclose()
        assert len(wire) == 2
        # Immediate second call must raise HttpDeferred without wire send
        with pytest.raises(HttpDeferred):
            await rec_client.get(f"{base_url}/poll2")
        assert len(wire) == 2
    finally:
        if owns:
            await rec_client.aclose()

    # Advance clock past spacing
    now[0] += spacing + 0.1

    # 3. Recovery HTTP client POST path
    post_client, post_owns = build_recovery_http_client(
        "direct", gateway=gateway, egress_group="default"
    )
    try:
        post_resp = await post_client.post(f"{base_url}/firewall/captcha/get", json={"token": "t"})
        assert post_resp.status_code == 200
        await post_resp.aclose()
        assert len(wire) == 3
        # Immediate second call before spacing raises HttpDeferred
        with pytest.raises(HttpDeferred):
            await post_client.post(f"{base_url}/firewall/captcha/get", json={"token": "t"})
        assert len(wire) == 3
    finally:
        if post_owns:
            await post_client.aclose()

    # Advance clock past spacing
    now[0] += spacing + 0.1

    # 4. Slot fetcher HTTPX path
    slot_cfg = SlotFetchConfig(base_url=base_url, timeout_sec=0.1)
    slot_fetcher = HttpSlotFetcher(
        slot_id="slot_gated", endpoint="direct",
        config=slot_cfg, gateway=gateway, egress_group="default",
    )
    try:
        fetch_res1 = await slot_fetcher.fetch(f"{base_url}/catalog")
        assert fetch_res1.status_code == 200
        assert len(wire) == 4
        # Immediate second fetch must raise HttpDeferred without wire send
        with pytest.raises(HttpDeferred):
            await slot_fetcher.fetch(f"{base_url}/catalog2")
        assert len(wire) == 4
    finally:
        await slot_fetcher.close()


@pytest.mark.asyncio
async def test_deferred_does_not_quarantine_or_damage_session(loopback_wire):
    base_url, wire = loopback_wire
    now = [200.0]
    budget = RequestBudget(enforce=True, spacing_sec=15.0, clock=lambda: now[0], shared_upstreams=True)
    gateway = RequestGateway(budget)

    # Pre-occupy budget
    first_client, owns = build_recovery_http_client(
        "direct", gateway=gateway, egress_group="default"
    )
    try:
        await first_client.get(f"{base_url}/init")
        assert len(wire) == 1
    finally:
        if owns:
            await first_client.aclose()

    # Now attempt challenge handoff with same gateway
    slot_repo = Mock()
    async def async_get_slot(sid):
        return SlotSnapshot(
            slot_id=sid, endpoint="direct", health=SlotHealth.READY,
            session_version=1, generation=1, role=SlotRole.ACTIVE,
        )
    slot_repo.get_slot = async_get_slot
    async def async_set_health(**kw):
        pass
    slot_repo.set_health = Mock(side_effect=async_set_health)
    session_store = Mock()

    service = ChallengeHandoffService(
        slot_repo=slot_repo, session_store=session_store,
        allow_browser=False, egress_gateway=gateway, egress_groups=EgressRegulatorConfig(),
    )
    req = ChallengeRequest(
        slot_id="slot_defer", challenge_token="tok", client_identity="user_b",
        expected_generation=1, endpoint="direct",
    )
    try:
        # Challenge must raise HttpDeferred because gateway budget is in cooldown
        with pytest.raises(HttpDeferred):
            await service.handle_challenge(req)
        # Slot must NOT have been quarantined!
        slot_repo.set_health.assert_not_called()
        # No extra wire call made
        assert len(wire) == 1
    finally:
        await service.close()
