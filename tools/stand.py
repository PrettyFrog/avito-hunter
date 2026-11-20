"""Unified local test stand orchestrating FakeTelegram, FakeAvito, and Hunter bot (<= 400 lines)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

from hunter.bootstrap import Bootstrap
from hunter.core import config as config_mod
from hunter.core import db as db_mod
from hunter.core.config import Settings
from hunter.core.db import Database
from hunter.notify.public_telegram_sender import PublicTelegramSender
from hunter.notify.telegram import TelegramChannel
from tools.fake_avito_server import FakeAvitoStand
from tools.fake_telegram_server import FakeTelegramStand, FakeUser


@dataclass
class StandContext:
    owner: FakeUser
    user1: FakeUser
    stranger: FakeUser
    avito: FakeAvitoStand
    tg: FakeTelegramStand
    bootstrap: Bootstrap
    channel: TelegramChannel
    db: Database
    temp_dir: Path
    _orig_db: Database | None = None
    _tg_task: asyncio.Task[Any] | None = None

    async def stop(self, cleanup: bool = True) -> None:
        try:
            await self.channel.stop_listener()
        except Exception:
            pass
        db_mod._db = self._orig_db
        if self._tg_task and not self._tg_task.done():
            self._tg_task.cancel()
            try:
                await asyncio.wait_for(self._tg_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if self.channel._bot and hasattr(self.channel._bot, "session"):
            try:
                await self.channel._bot.session.close()
            except Exception:
                pass
        await self.bootstrap.shutdown()
        if self.db:
            await self.db.close()
        await self.tg.stop()
        await self.avito.stop()
        os.environ.pop("HUNTER_STAND_TEST", None)
        os.environ.pop("HUNTER_DATA_DIR", None)
        os.environ.pop("HUNTER_PUBLIC_V2__ENABLED", None)
        config_mod.get_settings.cache_clear()
        if cleanup:
            shutil.rmtree(self.temp_dir, ignore_errors=True)


async def create_stand(
    *,
    owner_chat_id: int = 111111,
    user1_chat_id: int = 222222,
    stranger_chat_id: int = 999999,
    extra_settings: dict[str, Any] | None = None,
    existing_temp_dir: Path | None = None,
) -> StandContext:
    is_fresh = existing_temp_dir is None
    temp_dir = existing_temp_dir or Path(tempfile.mkdtemp(prefix="hunter_stand_"))
    db_path = temp_dir / "hunter.db"
    os.environ["HUNTER_STAND_TEST"] = "1"
    os.environ["HUNTER_DATA_DIR"] = str(temp_dir)
    os.environ["HUNTER_PUBLIC_V2__ENABLED"] = "true"
    config_mod.get_settings.cache_clear()
    if is_fresh:
        prod_src = Path("data/products")
        prod_dst = temp_dir / "products"
        if prod_src.exists():
            shutil.copytree(prod_src, prod_dst)

    tg = FakeTelegramStand()
    tg_url = await tg.start()

    avito = FakeAvitoStand()
    avito_url = await avito.start()

    settings_dict: dict[str, Any] = {
        "telegram_bot_token": "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_123456",
        "telegram_chat_id": str(owner_chat_id),
        "telegram_api_base": tg_url,
        "avito_base_url": avito_url,
        "public_v2": {
            "enabled": True,
            "slot_capacity": 4,
            "day_interval_sec": 1.0,
            "night_interval_sec": 2.0,
            "price_watch_enabled": False,
        },
        "http_proxy": None,
    }
    if extra_settings:
        settings_dict.update(extra_settings)

    settings = Settings.model_validate(settings_dict)
    orig_db = db_mod._db
    db = Database(db_path)
    db_mod._db = db
    await db.connect()
    await db.migrate()

    if is_fresh:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await db._conn.execute(
            "INSERT OR IGNORE INTO users (id, telegram_user_id, chat_id, username, role, status, created_at) "
            "VALUES (1, ?, ?, ?, 'owner', 'active', ?)",
            (owner_chat_id, owner_chat_id, "owner_user", now_iso),
        )
        await db._conn.execute(
            "INSERT OR IGNORE INTO channel_destinations (user_id, channel, address, verified, version, created_at) "
            "VALUES (1, 'telegram', ?, 1, 1, ?)",
            (str(owner_chat_id), now_iso),
        )
        await db._conn.execute(
            "INSERT OR IGNORE INTO slots (id, endpoint, account_id, cookie_namespace, health_state, active_generation, created_at) "
            "VALUES ('slot-stand', 'direct', '', 'stand', 'READY', 1, ?)",
            (now_iso,),
        )
        await db._conn.commit()

    channel = TelegramChannel(settings)
    bs = Bootstrap(settings, db=db)
    bs.senders["telegram"] = PublicTelegramSender(bs, channel)
    bs.owner_alert_sender = getattr(channel, "alert", None)

    await bs.startup()
    if hasattr(bs, "cutover_manager") and bs.cutover_manager:
        await bs.cutover_manager.ensure_v2_if_absent(1)

    tg_task = asyncio.create_task(
        channel.listen(db=bs.db, subscription_service=bs.subscription_service),
        name="stand-telegram-listener",
    )
    bs._tasks.append(tg_task)

    owner = tg.get_user(user_id=owner_chat_id, chat_id=owner_chat_id, username="owner_user")
    user1 = tg.get_user(user_id=user1_chat_id, chat_id=user1_chat_id, username="invited_user")
    stranger = tg.get_user(user_id=stranger_chat_id, chat_id=stranger_chat_id, username="stranger_user")

    # Give aiogram router half a second to initialize commands and start polling
    await asyncio.sleep(0.3)

    return StandContext(
        owner=owner,
        user1=user1,
        stranger=stranger,
        avito=avito,
        tg=tg,
        bootstrap=bs,
        channel=channel,
        db=db,
        temp_dir=temp_dir,
        _orig_db=orig_db,
        _tg_task=tg_task,
    )
