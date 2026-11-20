"""Adversarial tests for public URL and tenant authorization boundaries."""

from __future__ import annotations

import asyncio
from ipaddress import ip_address
import sqlite3
from pathlib import Path

import pytest
from aiogram.types import CallbackQuery, User as TgUser
from unittest.mock import AsyncMock

from hunter.core.models import User
from hunter.domain.query_url import (
    UntrustedHostError,
    UserinfoNotAllowedError,
    validate_and_canonicalize_query_url,
)
from hunter.notify.security_parts.authandscopemiddleware___call_01 import (
    check_registered_user_role,
)
from hunter.storage.subscriptions import (
    AccessDeniedError,
    SubscriptionActor,
    SubscriptionRepository,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/search",
        "https://0x7f000001/search",
        "https://0177.0.0.1/search",
        "https://[::1]/search",
        "https://[::ffff:127.0.0.1]/search",
        "https://169.254.169.254/latest/meta-data/",
        "https://avito.ru./search",
        "https://avito.ru@127.0.0.1/search",
        "https://avito.ru%2e.evil.example/search",
        "https://www.avito.ru:444/search",
    ],
)
def test_url_authority_rejects_ip_aliases_and_ambiguous_hosts(url: str) -> None:
    with pytest.raises((UntrustedHostError, UserinfoNotAllowedError, ValueError)):
        validate_and_canonicalize_query_url(url, 10000)


@pytest.mark.parametrize("hostname", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"])
def test_url_allowlist_is_independent_of_ip_classification(hostname: str) -> None:
    assert ip_address(hostname).is_loopback or ip_address(hostname).is_private or hostname == "::1"
    with pytest.raises((UntrustedHostError, ValueError)):
        validate_and_canonicalize_query_url(f"https://{hostname}/search", 10000)


@pytest.mark.asyncio
async def test_forged_callback_role_and_payload_cannot_grant_owner() -> None:
    attacker = User(
        id=22, telegram_user_id=222, chat_id=222, role="user", status="active", created_at=""
    )
    answer = AsyncMock()
    event = CallbackQuery(
        id="forged", from_user=TgUser(id=222, is_bot=False, first_name="attacker"),
        chat_instance="ci", data="owner:tenant:1",
    )
    object.__setattr__(event, "answer", answer)
    allowed = await check_registered_user_role(attacker, "owner", 222, "tenant_set", event)
    assert allowed is False
    answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_foreign_tenant_mutations_are_denied(app_setup) -> None:
    repo = app_setup
    await repo.create(SubscriptionActor(1), "sub_owner", 1, "q_owner", 10000, "", "https://avito.ru/q")

    async def forged_mutation() -> bool:
        try:
            await repo.update(SubscriptionActor(2), "sub_owner", expected_version=1, max_price=1)
        except AccessDeniedError:
            return False
        return True

    assert not any(await asyncio.gather(*(forged_mutation() for _ in range(20))))
    assert (await repo.get(SubscriptionActor(1), "sub_owner")).max_price == 10000


@pytest.fixture
async def app_setup(tmp_path):
    """Use real migrated schema and writer to exercise repository tenant checks."""
    from hunter.storage.connection import StorageConfig, StorageConnectionManager
    from hunter.storage.writer import SQLiteWriterActor

    db_path = tmp_path / "public-security.db"
    conn = sqlite3.connect(db_path)
    schema = Path(__file__).resolve().parents[2] / "src/hunter/core/schema.sql"
    conn.executescript(schema.read_text(encoding="utf-8"))
    conn.close()
    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    repo = SubscriptionRepository(mgr, writer)
    try:
        yield repo
    finally:
        await writer.stop()
        await mgr.drain_and_close()
