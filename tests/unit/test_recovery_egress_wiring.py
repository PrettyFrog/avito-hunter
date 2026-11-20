"""Owned recovery HTTP uses shared admission; borrowed client stays untouched."""
import httpx
import pytest
import asyncio

from hunter.domain.request_budget import RequestBudget
from hunter.transport.metered_http import HttpDeferred, RequestGateway, MeteredTransport
from hunter.transport.recovery_transport import build_recovery_http_client
from hunter.transport.managed_recovery_client import managed_recovery_client
from hunter.transport.challenge_models import ChallengeRequest
from hunter.transport.challenge_models import ChallengeOutcome, ChallengeStatus
from hunter.transport.challenge import ChallengeHandoffService
from hunter.core.egress_config import EgressRegulatorConfig
from hunter.transport.challenge_dispatch import resolve_firewall_captcha_dispatch


@pytest.mark.asyncio
async def test_owned_recovery_client_is_metered_and_borrowed_is_preserved():
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5))
    owned, owns = build_recovery_http_client("direct", gateway=gateway,
                                              egress_group="shared")
    try:
        assert owns and isinstance(owned._transport, MeteredTransport)
    finally:
        await owned.aclose()
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        same, owns = build_recovery_http_client("direct", client=borrowed,
                                                  gateway=gateway, egress_group="shared")
        assert same is borrowed and not owns
        assert not isinstance(borrowed._transport, MeteredTransport)
        assert not borrowed.is_closed
    finally:
        await borrowed.aclose()


@pytest.mark.asyncio
async def test_recovery_manager_rejects_ungated_borrowed_client():
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5))
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    req = ChallengeRequest(slot_id="slot", endpoint="direct", http_client=borrowed)
    try:
        with pytest.raises(RuntimeError, match="outside the egress gateway"):
            async with managed_recovery_client(req, "direct", gateway, EgressRegulatorConfig()):
                pass
        assert not borrowed.is_closed
    finally:
        await borrowed.aclose()


@pytest.mark.asyncio
async def test_captcha_dispatch_preserves_local_admission_deferral():
    class DeferredClient:
        async def post(self, *args, **kwargs):
            raise HttpDeferred("cooldown", 123.0)
    with pytest.raises(HttpDeferred):
        await resolve_firewall_captcha_dispatch(DeferredClient())


@pytest.mark.asyncio
async def test_service_coalesces_same_identity_generation_but_not_user_b():
    gateway = RequestGateway(RequestBudget(enforce=True, spacing_sec=7.5))
    service = ChallengeHandoffService(None, None, egress_gateway=gateway)
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    async def operation(req):
        calls.append(req.client_identity)
        started.set()
        await release.wait()
        return ChallengeOutcome(req.slot_id, ChallengeStatus.ERROR, False, None, False)
    service._handle_owned_challenge = operation
    a = ChallengeRequest("slot", client_identity="user_a", expected_generation=4)
    b = ChallengeRequest("slot", client_identity="user_b", expected_generation=4)
    first = asyncio.create_task(service.handle_challenge(a))
    await started.wait()
    duplicate = asyncio.create_task(service.handle_challenge(a))
    other = asyncio.create_task(service.handle_challenge(b))
    await asyncio.sleep(0)
    release.set()
    try:
        await asyncio.gather(first, duplicate, other)
        assert sorted(calls) == ["user_a", "user_b"]
    finally:
        await service.close()
