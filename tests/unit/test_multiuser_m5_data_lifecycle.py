from __future__ import annotations

from pathlib import Path

import pytest

from hunter.core.db import Database
from hunter.core.product import (
    ProductProfile,
    ProductStore,
    clear_tombstones,
    delete_product_profile,
    delete_user_products,
    is_tombstoned,
    save_product_profile,
)


@pytest.mark.asyncio
async def test_full_data_purge_matrix_and_table_traversal(tmp_path: Path) -> None:
    """Verifies complete deletion of user data across all tables and disk files,
    traversing all SQLite tables to ensure zero residual rows.
    """
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    # Create owner and regular user
    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )
    user_tg_id = 2002
    user_chat_id = 2002
    user_id = await db.create_user(
        telegram_user_id=user_tg_id,
        chat_id=user_chat_id,
        role="user",
        status="active",
        tier="friend",
    )

    # Populate data in various tables for user_id
    conn = await db.connect()

    # 1. support_tickets
    await db.create_support_ticket(user_id, user_chat_id, "Help me please")

    # 2. deliveries
    await conn.execute(
        """
        INSERT INTO events (id, avito_id, revision, kind, observed_at)
        VALUES ('evt_u', 'lot_u', 1, 'NEW_LISTING', datetime('now'))
        """
    )
    await conn.execute(
        """
        INSERT INTO deliveries (id, event_id, user_id, channel, status, available_at)
        VALUES ('del_u', 'evt_u', ?, 'telegram', 'pending', datetime('now'))
        """,
        (user_id,),
    )

    # 3. digest_queue
    await conn.execute(
        """
        INSERT INTO digest_queue (user_id, avito_id, payload, reason, created_at)
        VALUES (?, 'lot2', '{}', 'quiet_hours', datetime('now'))
        """,
        (user_id,),
    )

    # 5. pending_products
    await db.save_pending_product(user_chat_id, "await_url", {"test": True})

    # 6. user_events
    await db.log_user_event(user_id, "test_event", "details")

    # 7. access_requests
    await conn.execute(
        """
        INSERT INTO access_requests (telegram_user_id, username, display_name, status, created_at)
        VALUES (?, 'test_user', 'Test', 'pending', datetime('now'))
        """,
        (user_tg_id,),
    )

    # 8. invites created by user
    from datetime import timedelta

    from hunter.core.models import now_utc

    await db.create_invite(
        token_hash="hash123",
        label="Friend invite",
        tier="friend",
        created_by=user_id,
        expires_at=(now_utc() + timedelta(days=2)).isoformat(),
    )
    await conn.commit()

    # Products on disk
    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(directory=products_dir)

    p1 = ProductProfile(
        id="prod_user_1",
        name="User product 1",
        search_url="https://www.avito.ru/1",
        max_price=10000,
        owner_user_id=user_id,
    )
    save_product_profile(p1, directory=products_dir)
    ref_dir = products_dir / p1.id / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "sample.jpg").write_bytes(b"dummy image data")

    assert (products_dir / "prod_user_1.yaml").exists()
    assert (ref_dir / "sample.jpg").exists()

    # Execute deletion
    deleted_pids = await delete_user_products(user_id, store=store, db=db)
    assert "prod_user_1" in deleted_pids
    assert not (products_dir / "prod_user_1.yaml").exists()
    assert not (products_dir / "prod_user_1").exists()

    await db.delete_user_data(user_id)

    # Check user record: telegram_user_id and chat_id are nullified, status is deleted
    u = await db.get_user_by_id(user_id)
    assert u is not None
    assert u["status"] == "deleted"
    assert u["telegram_user_id"] is None
    assert u["chat_id"] is None

    # Exhaustive table traversal test across all tables
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    tables = [r[0] for r in await cur.fetchall()]

    user_matching_columns = [
        "user_id",
        "owner_user_id",
        "recipient_user_id",
        "sender_user_id",
        "created_by",
    ]

    for table in tables:
        col_cur = await conn.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in await col_cur.fetchall()]

        # Check user_id referencing columns
        for c in user_matching_columns:
            if c in cols:
                chk_cur = await conn.execute(
                    f"SELECT count(*) FROM {table} WHERE {c} = ?", (user_id,)
                )
                count = (await chk_cur.fetchone())[0]
                assert count == 0, f"Table {table} still has {count} rows with {c} = {user_id}"

        # Check telegram_user_id and chat_id columns outside users table
        if table != "users":
            if "telegram_user_id" in cols:
                chk_cur = await conn.execute(
                    f"SELECT count(*) FROM {table} WHERE telegram_user_id = ?", (user_tg_id,)
                )
                count = (await chk_cur.fetchone())[0]
                assert count == 0, (
                    f"Table {table} still has {count} rows with telegram_user_id = {user_tg_id}"
                )
            if "chat_id" in cols:
                chk_cur = await conn.execute(
                    f"SELECT count(*) FROM {table} WHERE chat_id = ?", (user_chat_id,)
                )
                count = (await chk_cur.fetchone())[0]
                assert count == 0, (
                    f"Table {table} still has {count} rows with chat_id = {user_chat_id}"
                )

    # Re-registration test: since telegram_user_id was nullified, user can register again!
    new_user_id = await db.create_user(
        telegram_user_id=user_tg_id,
        chat_id=user_chat_id,
        role="user",
        status="active",
        tier="friend",
    )
    assert new_user_id != user_id
    new_user = await db.get_user_by_telegram_id(user_tg_id)
    assert new_user is not None
    assert new_user["id"] == new_user_id
    assert new_user["status"] == "active"


@pytest.mark.asyncio
async def test_tombstones_prevent_saving_and_recreating(tmp_path: Path) -> None:
    """Verifies that tombstoned product IDs cannot be saved or loaded back from disk."""
    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(directory=products_dir)

    p = ProductProfile(
        id="tombstone_item_1",
        name="Tombstoned",
        search_url="https://avito.ru",
        max_price=5000,
        owner_user_id=42,
    )
    save_product_profile(p, directory=products_dir)
    assert (products_dir / "tombstone_item_1.yaml").exists()

    # Delete product
    delete_product_profile(p.id, directory=products_dir)
    assert not (products_dir / "tombstone_item_1.yaml").exists()
    assert is_tombstoned(p.id)

    # Attempting to save again must raise ValueError
    with pytest.raises(ValueError, match="Cannot save tombstoned product"):
        save_product_profile(p, directory=products_dir)

    # Attempting to save through ProductStore.save() must raise ValueError
    with pytest.raises(ValueError, match="Cannot save tombstoned product"):
        store.save(p)

    # Simulating an rogue or race-created YAML on disk: load_all must ignore & purge it
    rogue_yaml = products_dir / "tombstone_item_1.yaml"
    rogue_yaml.write_text(
        "id: tombstone_item_1\nname: Rogue\nsearch_url: https://avito.ru\nmax_price: 5000\n",
        encoding="utf-8",
    )
    assert rogue_yaml.exists()

    profiles, errors = store.load_all()
    assert "tombstone_item_1" not in profiles
    # Rogue file should be purged by load_all
    assert not rogue_yaml.exists()

    clear_tombstones()


def test_path_traversal_protection(tmp_path: Path) -> None:
    """Verifies that delete_product_profile strictly prevents path traversal."""
    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)

    # Create a sensitive file outside products directory
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("classified data")

    # Path traversal attempts
    with pytest.raises(ValueError, match="path traversal detected"):
        delete_product_profile("../secret.txt", directory=products_dir)

    with pytest.raises(ValueError, match="path traversal detected"):
        delete_product_profile("../../etc/passwd", directory=products_dir)

    with pytest.raises(ValueError, match="path traversal detected"):
        delete_product_profile("/etc/passwd", directory=products_dir)

    assert secret_file.exists()
    assert secret_file.read_text() == "classified data"


@pytest.mark.asyncio
async def test_owner_immunity(tmp_path: Path) -> None:
    """Verifies that owner cannot be deleted, blocked, or paused."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    await db.connect()

    owner_id = await db.create_user(
        telegram_user_id=1001, chat_id=1001, role="owner", status="active"
    )

    products_dir = tmp_path / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    store = ProductStore(directory=products_dir)

    p_owner = ProductProfile(
        id="owner_prod_1",
        name="Owner Product",
        search_url="https://avito.ru",
        max_price=20000,
        owner_user_id=owner_id,
    )
    save_product_profile(p_owner, directory=products_dir)

    # Attempt delete_user_products on owner must refuse with ValueError
    with pytest.raises(ValueError, match="Owner cannot be deleted, blocked, or paused"):
        await delete_user_products(owner_id, store=store, db=db)
    assert (products_dir / "owner_prod_1.yaml").exists()

    # Attempt delete_user_data on owner must refuse with ValueError
    with pytest.raises(ValueError, match="Owner cannot be deleted, blocked, or paused"):
        await db.delete_user_data(owner_id)

    owner_user = await db.get_user_by_id(owner_id)
    assert owner_user["status"] == "active"
    assert owner_user["telegram_user_id"] == 1001
