"""Unit tests for GeeTest solver client lifecycle, cancellation cleanup, and borrowed client preservation (T-980)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hunter.transport.geetest_solver import solve_geetest_v4_flow


@pytest.mark.asyncio
async def test_own_client_closed_on_cancel_during_initial_captcha() -> None:
    """When solve_geetest_v4_flow creates its own client and is cancelled during initial captcha, client is closed."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        with (
            patch("hunter.transport.geetest_solver._init_geetest_client", return_value=client),
            patch("hunter.transport.geetest_solver._get_firewall_captcha_id", new=AsyncMock(side_effect=asyncio.CancelledError())),
        ):
            with pytest.raises(asyncio.CancelledError):
                await solve_geetest_v4_flow()
        assert client.is_closed is True, "Own client was not closed after CancelledError!"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_borrowed_client_not_closed_on_cancel() -> None:
    """When a borrowed client is passed in, CancelledError must propagate without closing the borrowed client."""
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        with patch("hunter.transport.geetest_solver._get_firewall_captcha_id", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with pytest.raises(asyncio.CancelledError):
                await solve_geetest_v4_flow(client=borrowed)
        assert borrowed.is_closed is False, "Borrowed client must not be closed on cancellation!"
    finally:
        await borrowed.aclose()


@pytest.mark.asyncio
async def test_own_client_closed_on_error_during_initial_captcha() -> None:
    """When solve_geetest_v4_flow creates its own client and initial captcha raises, client is closed."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    try:
        with (
            patch("hunter.transport.geetest_solver._init_geetest_client", return_value=(client, True)),
            patch("hunter.transport.geetest_solver._get_firewall_captcha_id", new=AsyncMock(side_effect=RuntimeError("captcha network error"))),
        ):
            solved, cookies = await solve_geetest_v4_flow()
            assert solved is False
            assert cookies == []
        assert client.is_closed is True, "Own client was not closed after error!"
    finally:
        await client.aclose()
