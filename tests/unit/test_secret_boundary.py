"""Secrets are masked at configuration boundaries and remain usable by adapters."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hunter.core.config import Settings


def test_secret_settings_mask_repr_and_dump_but_preserve_runtime_access() -> None:
    values = {
        "telegram_bot_token": "fake-telegram-token",
        "smtp_password": "fake-smtp-password",
        "gigachat_credentials": "fake-gigachat-credentials",
        "gemini_api_key_primary": "fake-gemini-primary",
        "gemini_api_key": "fake-gemini-fallback",
        "openrouter_api_key": "fake-openrouter-key",
    }
    settings = Settings(_env_file=None, **values)

    rendered = repr(settings)
    dumped = repr(settings.model_dump())
    for secret in values.values():
        assert secret not in rendered
        assert secret not in dumped

    assert settings.telegram_bot_token.get_secret_value() == values["telegram_bot_token"]
    assert settings.smtp_password.get_secret_value() == values["smtp_password"]
    assert settings.gigachat_credentials.get_secret_value() == values["gigachat_credentials"]
    assert settings.gemini_keys == [values["gemini_api_key_primary"], values["gemini_api_key"]]
    assert settings.openrouter_api_key.get_secret_value() == values["openrouter_api_key"]


def test_secret_settings_load_from_environment_without_exposing_values(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "environment-test-token")
    settings = Settings(_env_file=None)
    assert settings.telegram_bot_token.get_secret_value() == "environment-test-token"
    assert "environment-test-token" not in repr(settings)


@pytest.mark.asyncio
async def test_gemini_key_is_sent_in_header_not_url(monkeypatch) -> None:
    from hunter.llm.router_parts import llmrouter__gemini_04 as gemini

    secret = "fake-gemini-api-key"
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            response = AsyncMock()
            response.status_code = 200
            response.json = lambda: {
                "candidates": [{"content": {"parts": [{"text": "{}"}]}}]
            }
            return response

    settings = Settings(_env_file=None, gemini_api_key=secret)
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini.httpx, "AsyncClient", Client)

    assert await gemini.execute_gemini_call("prompt", system="") == {}
    data, error, _model = await gemini.execute_gemini_vision_retry(
        {}, secret, "gemini-test", None
    )
    assert data is not None and error is None
    assert len(calls) == 2
    for url, kwargs in calls:
        assert secret not in url
        assert "key=" not in url
        assert kwargs["headers"]["x-goog-api-key"] == secret
