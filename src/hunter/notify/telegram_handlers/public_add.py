"""Public subscription Telegram add flow (S4 / T-474).

Zero-LLM guarantee:
- 3 user fields: Avito search URL, exact max price, optional stop-words.
- Tenant isolation across multiple parallel users.
- Dialogue cancellation via command, text, or inline button.
- Rejects conflicting or insecure search URLs without guessing.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from hunter.application.subscriptions import SubscriptionService
from hunter.core.models import User
from hunter.domain.public_price import parse_public_price
from hunter.domain.query_url import ConflictingPriceError, validate_and_canonicalize_query_url
from hunter.notify.security import requires
from hunter.storage.connection import StorageConfig, StorageConnectionManager
from hunter.storage.subscriptions import SubscriptionRepository, validate_subscription_name
from hunter.storage.writer import SQLiteWriterActor


async def ensure_public_tenant_selector(db: Any, user_id: int, service: Any) -> None:
    pass


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="public_add_cancel")]]
    )


def _words_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="public_add_skip_words")],
            [InlineKeyboardButton(text="Отмена", callback_data="public_add_cancel")],
        ]
    )


async def start_public_add(message: Message, db: Any, user: User) -> None:
    """Initiates the public subscription wizard."""
    await ensure_public_tenant_selector(db, user.id, getattr(db, "subscription_service", None))
    chat_id = message.chat.id
    payload = {"user_id": user.id}
    await db.save_pending_product(
        chat_id, "public_add_name", payload, ttl_hours=24, owner_user_id=user.id
    )
    await message.reply(
        "➕ <b>Добавление подписки</b>\n\n"
        "Шаг 1 из 4: Как назвать этот поиск?",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


async def _handle_name_step(
    message: Message, db: Any, user: User, chat_id: int, payload: dict, user_text: str
) -> bool:
    try:
        payload["name"] = validate_subscription_name(user_text)
    except ValueError as exc:
        await message.reply(f"❌ {exc}. Попробуйте снова:", reply_markup=_cancel_kb())
        return True
    await db.save_pending_product(
        chat_id, "public_add_url", payload, ttl_hours=24, owner_user_id=user.id
    )
    await message.reply(
        "Шаг 2 из 4: Отправьте ссылку на поиск Авито (HTTPS, avito.ru):",
        reply_markup=_cancel_kb(),
    )
    return True


async def _handle_url_step(
    message: Message, db: Any, user: User, chat_id: int, payload: dict, user_text: str
) -> bool:
    p = urlparse(user_text)
    host = (p.hostname or "").lower()
    if (
        p.scheme.lower() != "https"
        or "@" in p.netloc
        or not (host in ("avito.ru", "www.avito.ru", "m.avito.ru") or host.endswith(".avito.ru"))
    ):
        await message.reply(
            "❌ Некорректная ссылка на Авито. Поддерживаются только поисковые ссылки "
            "https://www.avito.ru/...\nПопробуйте снова:",
            reply_markup=_cancel_kb(),
        )
        return True

    payload["url"] = user_text
    await db.save_pending_product(
        chat_id, "public_add_price", payload, ttl_hours=24, owner_user_id=user.id
    )
    await message.reply(
        "Шаг 3 из 4: Укажите максимальную цену (в рублях целым числом):",
        reply_markup=_cancel_kb(),
    )
    return True


async def _handle_price_step(
    message: Message, db: Any, user: User, chat_id: int, payload: dict, user_text: str
) -> bool:
    try:
        price = parse_public_price(user_text)
    except ValueError:
        await message.reply(
            "❌ Неверный формат цены. Укажите целое положительное число рублей:",
            reply_markup=_cancel_kb(),
        )
        return True

    try:
        validate_and_canonicalize_query_url(payload["url"], price)
    except ConflictingPriceError as exc:
        await message.reply(f"❌ {exc}. Укажите корректную цену:", reply_markup=_cancel_kb())
        return True
    except Exception as exc:
        await message.reply(
            f"❌ Некорректная ссылка или цена: {exc}. Попробуйте снова:", reply_markup=_cancel_kb()
        )
        return True

    payload["price"] = price
    await db.save_pending_product(
        chat_id, "public_add_words", payload, ttl_hours=24, owner_user_id=user.id
    )
    await message.reply(
        "Шаг 4 из 4: Укажите минус-слова через запятую (например: <i>битый, запчасти</i>).\n"
        "Или отправьте <b>-</b> / нажмите кнопку «Пропустить», если минус-слова не требуются:",
        parse_mode="HTML",
        reply_markup=_words_kb(),
    )
    return True


async def handle_public_add_message(
    message: Message, db: Any, user: User, sub_service: Any = None
) -> bool:
    """Processes message if user is in an active public add flow step. Returns True if handled."""
    chat_id = message.chat.id
    state = await db.get_pending_product(chat_id)
    if not state or not str(state.get("step", "")).startswith("public_add_"):
        return False

    if state.get("owner_user_id") and state["owner_user_id"] != user.id and user.role != "owner":
        return False

    step = state["step"]
    payload = state.get("payload", {})
    user_text = (message.text or "").strip()

    if user_text.lower() in ("/cancel", "отмена", "cancel"):
        await db.delete_pending_product(chat_id)
        await message.reply("❌ Добавление подписки отменено.")
        return True

    if step == "public_add_name":
        return await _handle_name_step(message, db, user, chat_id, payload, user_text)

    if step == "public_add_url":
        return await _handle_url_step(message, db, user, chat_id, payload, user_text)

    if step == "public_add_price":
        return await _handle_price_step(message, db, user, chat_id, payload, user_text)

    if step == "public_add_words":
        words = "" if user_text in ("-", "пропустить", "skip", "") else user_text
        await _finish_public_add(
            message, db, user, payload["url"], payload["price"], words, sub_service,
            name=payload.get("name"),
        )
        return True

    return False


async def _get_public_subscription_service(db: Any, sub_service: Any) -> Any:
    service = sub_service or getattr(db, "subscription_service", None)
    if service:
        return service

    mgr = getattr(db, "storage_mgr", None) or StorageConnectionManager(
        StorageConfig(db_path=Path(getattr(db, "path", "data/hunter.db")))
    )
    writer = getattr(db, "writer", None)
    if not writer:
        writer = SQLiteWriterActor(mgr)
        await writer.start()
        if hasattr(db, "writer"):
            db.writer = writer
    elif not writer.is_running:
        await writer.start()
    service = SubscriptionService(SubscriptionRepository(mgr, writer))
    if hasattr(db, "subscription_service"):
        db.subscription_service = service
    return service


async def _finish_public_add(
    message_or_cb: Any,
    db: Any,
    user: User,
    url: str,
    price: int,
    words: str,
    sub_service: Any = None,
    *,
    name: str | None = None,
) -> None:
    chat_id = (
        message_or_cb.chat.id if hasattr(message_or_cb, "chat") else message_or_cb.message.chat.id
    )

    service = await _get_public_subscription_service(db, sub_service)

    reply_fn = (
        message_or_cb.reply if hasattr(message_or_cb, "reply") else message_or_cb.message.reply
    )
    try:
        sub = await service.add_subscription(
            user_id=user.id,
            url=url,
            max_price=price,
            manual_stopwords=words,
            is_owner=(user.role == "owner"),
            name=name,
        )
    except Exception as exc:
        logger.error("Failed to add subscription in public wizard: {}", exc)
        await reply_fn(
            f"❌ <b>Ошибка сохранения подписки:</b> {html.escape(str(exc))}\n\n"
            "Попробуйте ещё раз или отмените /cancel.",
            parse_mode="HTML",
        )
        return

    await db.delete_pending_product(chat_id)

    words_desc = html.escape(sub.manual_stopwords) if sub.manual_stopwords else "нет"
    await reply_fn(
        f"✅ <b>Подписка успешно добавлена!</b>\n\n"
        f"🎯 Потолок цены: <b>{sub.max_price:,} ₽</b> (строгий, без запаса)\n"
        f"🚫 Минус-слова: <code>{words_desc}</code>\n"
        f"Название: <b>{html.escape(getattr(sub, 'name', '') or 'Без названия')}</b>\n\n"
        "Новые объявления и изменения цены появятся здесь после проверки поиска.",
        parse_mode="HTML",
    )


def _register_public_cancel_command(router: Router, db_resolver: Any, sub_service: Any) -> None:
    @router.message(Command("cancel"))
    @requires("user")
    async def cmd_public_cancel(message: Message, current_user: User | None = None) -> None:
        db = db_resolver() if callable(db_resolver) else db_resolver
        u = current_user or User(
            id=message.chat.id,
            telegram_user_id=message.chat.id,
            chat_id=message.chat.id,
            role="user",
            status="active",
            created_at="",
        )
        from hunter.notify.telegram_handlers.public_search_edit import (
            handle_public_add_message as dispatch_dialog,
        )

        await dispatch_dialog(message, db, u, sub_service)


def _register_public_add_cancel_callback(router: Router, db_resolver: Any) -> None:
    @router.callback_query(F.data == "public_add_cancel")
    @requires("user")
    async def cb_public_add_cancel(
        callback: CallbackQuery, current_user: User | None = None
    ) -> None:
        db = db_resolver() if callable(db_resolver) else db_resolver
        if callback.message:
            chat_id = callback.message.chat.id
            state = await db.get_pending_product(chat_id)
            if state and state.get("owner_user_id"):
                caller_id = (
                    current_user.id
                    if current_user
                    else (callback.from_user.id if callback.from_user else None)
                )
                caller_role = getattr(current_user, "role", "user")
                if caller_id and state["owner_user_id"] != caller_id and caller_role != "owner":
                    await callback.answer("Чужая сессия добавления.", show_alert=True)
                    return
            await db.delete_pending_product(chat_id)
            await callback.message.reply("❌ Добавление подписки отменено.")
        await callback.answer()


def _register_public_add_skip_callback(router: Router, db_resolver: Any, sub_service: Any) -> None:
    @router.callback_query(F.data == "public_add_skip_words")
    @requires("user")
    async def cb_public_add_skip(callback: CallbackQuery, current_user: User | None = None) -> None:
        if not callback.message:
            await callback.answer()
            return
        db = db_resolver() if callable(db_resolver) else db_resolver
        chat_id = callback.message.chat.id
        state = await db.get_pending_product(chat_id)
        if not state or state.get("step") != "public_add_words":
            await callback.answer()
            return
        if state.get("owner_user_id"):
            caller_id = (
                current_user.id
                if current_user
                else (callback.from_user.id if callback.from_user else None)
            )
            caller_role = getattr(current_user, "role", "user")
            if caller_id and state["owner_user_id"] != caller_id and caller_role != "owner":
                await callback.answer("Чужая сессия добавления.", show_alert=True)
                return
        p = state.get("payload", {})
        u = current_user or User(
            id=p.get("user_id", 1),
            telegram_user_id=chat_id,
            chat_id=chat_id,
            role="user",
            status="active",
            created_at="",
        )
        await _finish_public_add(
            callback, db, u, p.get("url", ""), p.get("price", 0), "", sub_service,
            name=p.get("name"),
        )
        await callback.answer()


def register_public_add_callbacks(
    router: Router, db_resolver: Any = None, sub_service: Any = None
) -> None:
    """Registers callback queries and cancel command for public add flow."""
    _register_public_cancel_command(router, db_resolver, sub_service)
    _register_public_add_cancel_callback(router, db_resolver)
    _register_public_add_skip_callback(router, db_resolver, sub_service)
