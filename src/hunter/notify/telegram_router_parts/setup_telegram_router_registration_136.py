"""Leaf helpers for setup_telegram_router registration slice 136."""

from __future__ import annotations

import html
import logging
import re
import shutil
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hunter.core.models import User, now_utc
from hunter.core.product import (
    ProductStore,
    apply_product_pause,
    clear_product_pause,
    save_product_profile,
)
from hunter.normalize.avito_url import normalize_avito_url
from hunter.notify.broadcast import set_global_pause_with_broadcast
from hunter.notify.messages_ru import (
    PAUSE_ALL_SUCCESS_TEMPLATE,
    PRODUCT_NOT_FOUND,
    RESUME_ALL_OWNER_DONE,
    RESUME_ALL_SUCCESS_TEMPLATE,
)
from hunter.notify.security import get_owned_product

logger = logging.getLogger(__name__)


async def handle_resume_all_command(message: Any, db: Any, bot: Any = None) -> None:
    await set_global_pause_with_broadcast(db, paused=False, bot=bot)
    await message.reply(RESUME_ALL_OWNER_DONE)


def _pause_store_products(store: Any, user_id: int, settings: Any) -> int:
    if store is None or not hasattr(store, "get_for_user"):
        return 0
    count = 0
    for p in store.get_for_user(user_id).values():
        if p.mode != "off":
            p.mode = "off"
            p.paused_reason = "user"
            save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
            count += 1
    return count


def _resume_store_products(store: Any, current_user: Any, settings: Any) -> int:
    if store is None or not hasattr(store, "get_for_user"):
        return 0
    count = 0
    for p in store.get_for_user(current_user.id).values():
        if p.mode == "off":
            ok, _ = clear_product_pause(p, actor_role=current_user.role)
            if ok:
                p.last_confirmed_at = now_utc().isoformat()
                save_product_profile(p, directory=getattr(store, "directory", settings.data_dir / "products"))
                count += 1
    return count


async def handle_pause_all_confirm_callback(
    callback: Any, current_user: User, store: Any, settings: Any, db: Any = None
) -> None:
    count = 0
    if db is not None:
        try:
            from hunter.notify.telegram_handlers.public_status import _get_repo, pause_subscription
            from hunter.storage.subscriptions import SubscriptionActor

            repo = _get_repo(db)
            actor = SubscriptionActor(user_id=current_user.id, is_owner=(current_user.role == "owner"))
            subs = await repo.list_for_actor(actor)
            for s in subs:
                if s.enabled:
                    ok, _ = await pause_subscription(current_user, s.id, db)
                    if ok:
                        count += 1
        except Exception as exc:
            logger.warning("bulk pause failed to list subscriptions: %s", exc)

    if count == 0:
        count = _pause_store_products(store, current_user.id, settings)

    if callback.message:
        await callback.message.edit_text(PAUSE_ALL_SUCCESS_TEMPLATE.format(count=count))
    await callback.answer()


async def handle_pause_all_cancel_callback(callback: Any) -> None:
    if callback.message:
        await callback.message.edit_text("Действие отменено.")
    await callback.answer()


async def handle_resume_all_confirm_callback(
    callback: Any, current_user: User, store: Any, settings: Any, db: Any = None
) -> None:
    count = 0
    if db is not None:
        try:
            from hunter.notify.telegram_handlers.public_status import _get_repo, resume_subscription
            from hunter.storage.subscriptions import SubscriptionActor

            repo = _get_repo(db)
            actor = SubscriptionActor(user_id=current_user.id, is_owner=(current_user.role == "owner"))
            subs = await repo.list_for_actor(actor)
            for s in subs:
                if not s.enabled:
                    ok, _ = await resume_subscription(current_user, s.id, db)
                    if ok:
                        count += 1
        except Exception as exc:
            logger.warning("bulk resume failed to list subscriptions: %s", exc)

    if count == 0:
        count = _resume_store_products(store, current_user, settings)

    if callback.message:
        await callback.message.edit_text(RESUME_ALL_SUCCESS_TEMPLATE.format(count=count))
    await callback.answer()


async def handle_resume_all_cancel_callback(callback: Any) -> None:
    if callback.message:
        await callback.message.edit_text("Действие отменено.")
    await callback.answer()


async def handle_remove_command(
    message: Any,
    current_user: User,
    store: Any,
    db: Any,
    safe_reply_fn: Any,
) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Использование: `/remove <product_id>`")
        return
    pid = parts[1].strip()
    if pid.startswith("sub_"):
        from hunter.notify.telegram_handlers.public_status import delete_subscription
        _, msg = await delete_subscription(current_user, pid, db)
        await message.reply(msg, parse_mode="HTML")
        return
    if store is None:
        from hunter.products.store import ProductStore
        store = ProductStore()
    p = get_owned_product(current_user, pid, store)
    if not p:
        await message.reply(PRODUCT_NOT_FOUND)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Подтвердить удаление", callback_data=f"rm_ok:{pid}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"rm_no:{pid}"),
            ]
        ]
    )
    await safe_reply_fn(
        message,
        f"⚠️ <b>Вы уверены, что хотите удалить товар <code>{html.escape(p.name)}</code> (<code>{html.escape(pid)}</code>)?</b>\n"
        f"Будут удалены профиль и связанные эталоны.",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def handle_remove_confirm_callback(
    query: Any, current_user: User, settings: Any
) -> None:
    pid = (query.data or "").split(":", 1)[1]
    store = ProductStore(directory=settings.data_dir / "products")
    p = get_owned_product(current_user, pid, store)
    if not p:
        await query.answer(PRODUCT_NOT_FOUND, show_alert=True)
        return
    yaml_file = settings.data_dir / "products" / f"{pid}.yaml"
    ref_dir = settings.data_dir / "products" / pid
    if yaml_file.exists():
        yaml_file.unlink()
    if ref_dir.exists():
        shutil.rmtree(ref_dir, ignore_errors=True)
    if query.message:
        await query.message.edit_text(f"🗑 Товар `{pid}` успешно удален.")
    await query.answer("Удалено")


async def handle_remove_cancel_callback(
    query: Any, current_user: User, settings: Any
) -> None:
    pid = (query.data or "").split(":", 1)[1]
    store = ProductStore(directory=settings.data_dir / "products")
    p = get_owned_product(current_user, pid, store)
    if not p:
        await query.answer(PRODUCT_NOT_FOUND, show_alert=True)
        return
    if query.message:
        await query.message.edit_text(f"❌ Удаление товара `{pid}` отменено.")
    await query.answer("Отменено")


async def handle_edit_subscription_if_needed(
    message: Any, current_user: User, db: Any
) -> bool:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) >= 2 and parts[1].strip().startswith("sub_"):
        from hunter.notify.telegram_handlers.public_status import edit_subscription
        sub_id = parts[1].strip()
        rest = parts[2].strip() if len(parts) > 2 else ""
        p_val = int(rest.split("=")[1].strip()) if "price=" in rest else (int(rest) if rest.isdigit() else None)
        w_val = rest.split("=")[1].strip() if "words=" in rest or "stopwords=" in rest else (rest if not rest.isdigit() and rest else None)
        _, msg = await edit_subscription(current_user, sub_id, p_val, w_val, db)
        await message.reply(msg, parse_mode="HTML")
        return True
    return False


async def handle_edit_max_price(
    message: Any, p: Any, pid: str, field_val: str, settings: Any
) -> bool:
    new_price = int(re.sub(r"\D", "", field_val))
    if new_price <= 0:
        raise ValueError("Цена должна быть больше 0")
    if p.search_url:
        clean_url, url_err = normalize_avito_url(
            p.search_url, new_price, headroom_pct=getattr(p, "headroom_pct", 0.20) or 0.20
        )
        if url_err:
            await message.reply(f"❌ Ошибка нормализации URL: {url_err}")
            return True
        p.search_url = clean_url
    p.max_price = new_price
    save_product_profile(p, directory=settings.data_dir / "products")
    headroom_val = int(new_price * (1 + (p.headroom_pct or 0.20)))
    await message.reply(
        f"✅ Потолок цены товара `{pid}` успешно обновлен на {new_price:,} ₽. "
        f"Поисковый URL перегенерирован (pmax={headroom_val:,})."
    )
    return True
