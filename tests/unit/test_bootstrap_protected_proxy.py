from unittest.mock import AsyncMock

import pytest

from hunter.bootstrap import Bootstrap
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.transport.proxy_credentials import ProxyCredentialError


@pytest.mark.asyncio
async def test_enabled_protected_proxy_mode_wires_store_and_requires_credentials(tmp_path):
    settings = Settings(env="test")
    settings.public_v2.enabled = True
    settings.public_v2.protected_proxy_auth = True
    bootstrap = Bootstrap(
        settings, db=Database(tmp_path / "test.db"), senders={"telegram": AsyncMock()}
    )
    await bootstrap.startup()
    try:
        assert bootstrap.http_pool.credential_store is bootstrap.proxy_credential_store
        with pytest.raises(ProxyCredentialError):
            bootstrap.http_pool.get_fetcher("missing", "http://proxy:8080")
    finally:
        await bootstrap.shutdown()
