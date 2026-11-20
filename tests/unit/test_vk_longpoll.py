"""VK Long Poll transport validates servers and acknowledges offsets explicitly."""

from __future__ import annotations

import httpx
import pytest

from hunter.core.config import Settings
from hunter.transport.vk_longpoll import VkLongPollClient, VkLongPollError


def test_vk_listener_configuration_requires_all_credentials() -> None:
    with pytest.raises(ValueError, match="public_v2.enabled"):
        Settings(_env_file=None, vk_binding_enabled=True)
    with pytest.raises(ValueError, match="VK_GROUP_TOKEN"):
        Settings(
            _env_file=None,
            vk_binding_enabled=True,
            vk_group_id=42,
            public_v2={"enabled": True},
            channel_binding_secret="private-binding-secret-1234567890",
        )
    with pytest.raises(ValueError, match="VK_GROUP_TOKEN"):
        Settings(
            _env_file=None,
            vk_binding_enabled=True,
            vk_group_id=42,
            vk_group_token="  ",
            public_v2={"enabled": True},
            channel_binding_secret="private-binding-secret-1234567890",
        )


def test_longpoll_constructor_rejects_empty_credentials() -> None:
    with pytest.raises(ValueError):
        VkLongPollClient(0, "")


@pytest.mark.asyncio
async def test_longpoll_offset_advances_only_after_ack() -> None:
    poll_ts: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = {"response": {"server": "https://lp.vk.com/longpoll", "key": "fake", "ts": "7"}}
        else:
            poll_ts.append(request.url.params["ts"])
            body = {"ts": str(8 + len(poll_ts)), "updates": []}
        return httpx.Response(200, json=body, request=request)

    client = VkLongPollClient(
        42, "fake-token", client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    try:
        first = await client.poll()
        second = await client.poll()
        assert poll_ts == ["7", "7"]
        client.ack(second)
        await client.poll()
        assert poll_ts == ["7", "7", "10"]
        client.ack(first)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_expired_poll_key_is_refreshed_without_advancing_old_offset() -> None:
    server_calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal server_calls
        if request.method == "POST":
            server_calls += 1
            body = {
                "response": {
                    "server": "https://lp.vk.com/longpoll",
                    "key": f"key-{server_calls}",
                    "ts": str(server_calls),
                }
            }
        elif server_calls == 1:
            body = {"failed": 2}
        else:
            body = {"ts": "4", "updates": []}
        return httpx.Response(200, json=body, request=request)

    client = VkLongPollClient(
        42, "fake-token", client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    try:
        assert (await client.poll()).next_ts is None
        assert (await client.poll()).next_ts == "4"
        assert server_calls == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_untrusted_server_is_sanitized() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": {"server": "https://evil.example/lp", "key": "secret", "ts": 1}},
            request=request,
        )

    client = VkLongPollClient(
        42,
        "sensitive-access-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        with pytest.raises(VkLongPollError) as error:
            await client.poll()
        assert "sensitive-access-token" not in str(error.value)
        assert "evil.example" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_error_body_is_not_exposed() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"error_msg": "sensitive token rejected"}},
            request=request,
        )

    client = VkLongPollClient(
        42,
        "fake-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        with pytest.raises(VkLongPollError) as error:
            await client.poll()
        assert str(error.value) == "VK Long Poll returned an API error"
        assert "sensitive" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_malformed_server_port_is_rejected_without_echoing_server() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        body = {
            "response": {
                "server": "https://lp.vk.com:invalid/longpoll",
                "key": "fake-key",
                "ts": 1,
            }
        }
        return httpx.Response(200, json=body, request=request)

    client = VkLongPollClient(
        42,
        "fake-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        with pytest.raises(VkLongPollError) as error:
            await client.poll()
        assert str(error.value) == "VK returned an invalid Long Poll server"
        assert "lp.vk.com" not in str(error.value)
    finally:
        await client.close()
