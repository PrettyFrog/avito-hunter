import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import httpx

from hunter.notify.telegram_delivery import fetch_photo_bytes
from hunter.notify.public_telegram_sender import PublicTelegramSender


def test_public_telegram_sender_photo_client_trust_env():
    sender = PublicTelegramSender(bootstrap=None, channel=None)
    client = sender._get_photo_client()
    assert client.trust_env is False


@pytest.mark.asyncio
async def test_fetch_photo_bytes_trust_env(monkeypatch):
    # Set a bogus HTTP_PROXY
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:99999")
    
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_resp = MagicMock(status_code=200, content=b"fake_image_bytes")
        mock_instance.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_instance
        
        data = await fetch_photo_bytes("https://b00.img.avito.st/image/1/test.jpg")
        assert data == b"fake_image_bytes"
        
        # Verify httpx.AsyncClient was created with trust_env=False
        assert mock_client_cls.called
        kwargs = mock_client_cls.call_args.kwargs
        assert kwargs.get("trust_env") is False
