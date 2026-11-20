import os
import asyncio

import httpx
import pytest

from hunter.transport.http_fetch import SlotHttpPool
from hunter.transport.proxy_credentials import ProxyCredentialError, ProxyCredentialStore


def test_pool_supplies_separate_proxy_auth(tmp_path, monkeypatch):
    store = ProxyCredentialStore(tmp_path / "secrets")
    source = tmp_path / "input"
    source.write_text('{"username":"operator","password":"secret"}')
    fd = os.open(source, os.O_RDONLY)
    try:
        store.import_fd("slot", "http://proxy.example:8080", fd)
    finally:
        os.close(fd)
    captured = {}

    class FakeClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    pool = SlotHttpPool(credential_store=store)
    fetcher = pool.get_fetcher("slot", "http://proxy.example:8080")
    try:
        proxy = captured["proxy"]
        assert isinstance(proxy, httpx.Proxy)
        assert proxy.auth == ("operator", "secret")
        assert "secret" not in str(proxy.url)
        assert "secret" not in repr(fetcher)
    finally:
        asyncio.run(fetcher.close())


def test_pool_rejects_missing_credentials_before_creating_client(tmp_path, monkeypatch):
    store = ProxyCredentialStore(tmp_path / "secrets")

    def unexpected_client(**_kwargs):
        raise AssertionError("client must not start without credentials")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    pool = SlotHttpPool(credential_store=store)
    with pytest.raises(ProxyCredentialError):
        pool.get_fetcher("slot", "http://proxy.example:8080")
