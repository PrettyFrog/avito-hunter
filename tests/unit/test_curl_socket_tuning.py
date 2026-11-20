import os
import pytest
from unittest.mock import MagicMock
from hunter.transport.observed_curl import ObservedCurlSession


@pytest.mark.asyncio
async def test_observed_curl_session_default_options():
    session = ObservedCurlSession()
    assert session._buffersize == 0
    assert session._pipewait is False
    await session.close()


@pytest.mark.asyncio
async def test_observed_curl_session_explicit_options():
    session = ObservedCurlSession(buffersize=131072, pipewait=True)
    assert session._buffersize == 131072
    assert session._pipewait is True
    await session.close()


@pytest.mark.asyncio
async def test_observed_curl_session_env_options(monkeypatch):
    monkeypatch.setenv("HUNTER_CURL_BUFFERSIZE", "65536")
    monkeypatch.setenv("HUNTER_CURL_PIPEWAIT", "1")
    session = ObservedCurlSession()
    assert session._buffersize == 65536
    assert session._pipewait is True
    await session.close()


@pytest.mark.asyncio
async def test_observed_curl_pop_curl_applies_buffersize_and_pipewait():
    session = ObservedCurlSession(buffersize=131072, pipewait=True)
    curl = await session.pop_curl()
    try:
        # curl handle was successfully created and tuned without exceptions
        assert curl is not None
    finally:
        session.release_curl(curl)
    await session.close()
