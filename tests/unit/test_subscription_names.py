"""Per-tenant subscription names survive reads and concurrent updates."""
import asyncio
import sqlite3

import pytest
from tests.unit.test_subscription_repository import SCHEMA

from hunter.application.subscriptions import SubscriptionService
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.subscriptions import (
    AccessDeniedError,
    StaleSubscriptionVersionError,
    SubscriptionActor,
    SubscriptionRepository,
)
from hunter.storage.writer import SQLiteWriterActor


@pytest.fixture
async def names_env(tmp_path):
    path = tmp_path / "names.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    mgr = StorageConnectionManager(StorageConfig(db_path=path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        yield SubscriptionRepository(mgr, writer), writer, mgr, path
    finally:
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_same_query_has_independent_persistent_names(names_env):
    repo, _, _, _ = names_env
    service = SubscriptionService(repo)
    first = await service.add_subscription(101, "https://avito.ru/moskva?q=mac", 50000,
                                           sub_id="sub_a", name="Ноутбук")
    second = await service.add_subscription(102, "https://avito.ru/moskva?q=mac", 50000,
                                            sub_id="sub_b", name="Для работы")
    assert first.query_id == second.query_id
    assert (await repo.get(SubscriptionActor(101), "sub_a")).name == "Ноутбук"
    assert (await repo.list_for_actor(SubscriptionActor(102)))[0].name == "Для работы"
    with pytest.raises(AccessDeniedError):
        await repo.update(SubscriptionActor(102), "sub_a", 1, name="Чужое")
    renamed = await repo.update(SubscriptionActor(101), "sub_a", 1, name="Для дома")
    assert renamed.name == "Для дома" and renamed.version == 2
    with pytest.raises(StaleSubscriptionVersionError):
        await repo.update(SubscriptionActor(101), "sub_a", 1, name="Старое")
    assert (await repo.get(SubscriptionActor(102), "sub_b")).name == "Для работы"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "  ", "x" * 101, "line\nbreak"])
async def test_invalid_user_names_are_rejected(names_env, name):
    repo, _, _, _ = names_env
    with pytest.raises(ValueError):
        await SubscriptionService(repo).add_subscription(
            101, "https://avito.ru/moskva?q=mac", 50000, sub_id="sub_bad", name=name
        )


@pytest.mark.asyncio
async def test_rename_storage_fault_leaves_old_name_and_version(names_env):
    repo, writer, _, _ = names_env
    actor = SubscriptionActor(101)
    await repo.create(actor, "sub_fault", 101, "q_fault", 50000, name="Старое")
    await writer.submit(lambda c: c.execute(
        "CREATE TRIGGER fail_name BEFORE UPDATE ON subscriptions "
        "BEGIN SELECT RAISE(ABORT, 'injected name fault'); END"
    ))
    with pytest.raises(sqlite3.IntegrityError, match="injected name fault"):
        await repo.update(actor, "sub_fault", 1, name="Новое")
    original = await repo.get(actor, "sub_fault")
    assert original.name == "Старое" and original.version == 1


@pytest.mark.asyncio
async def test_competing_renames_do_not_overwrite_newer_version(names_env):
    repo, _, _, path = names_env
    actor = SubscriptionActor(101)
    await repo.create(actor, "sub_race", 101, "q_race", 50000, name="Начало")
    results = await asyncio.gather(
        repo.update(actor, "sub_race", 1, name="Первое"),
        repo.update(actor, "sub_race", 1, name="Второе"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, StaleSubscriptionVersionError) for result in results) == 1
    with sqlite3.connect(path) as conn:
        name, version = conn.execute(
            "SELECT name, version FROM subscriptions WHERE id = 'sub_race'"
        ).fetchone()
    assert name in ("Первое", "Второе") and version == 2
