"""Helper functions for on_dialog_text (slice 201)."""

from __future__ import annotations

import html
import re
from typing import Any

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from hunter.core.models import User
from hunter.core.product import save_product_profile
from hunter.normalize.user_input import extract_avito_search_url, parse_price_input
from hunter.notify.broadcast import get_broadcast_target_counts
from hunter.notify.messages_ru import (
    ADD_CANCEL_BTN,
    ADD_INVALID_PRICE,
    ADD_INVALID_URL,
    ADD_WAITING_TEMPLATE,
    BROADCAST_BTN_ALL,
    BROADCAST_BTN_CANCEL,
    BROADCAST_BTN_FAMILY,
    BROADCAST_BTN_FRIEND,
    BROADCAST_PREVIEW_TEMPLATE,
    MY_PRICE_UPDATED_TEMPLATE,
    MY_PRODUCT_ALREADY_DELETED,
    SUPPORT_ALERT_OWNER_TEMPLATE,
    SUPPORT_BTN_REPLY,
    SUPPORT_DELIVERY_FAILED_BLOCKED,
    SUPPORT_DELIVERY_FAILED_DELETED,
    SUPPORT_RECEIVED_TEMPLATE,
    SUPPORT_REPLY_SUCCESS_TEMPLATE,
    SUPPORT_TOO_LONG,
    SUPPORT_USER_REPLY_TEMPLATE,
)
from hunter.notify.telegram_handlers.public_search_edit import handle_public_add_message
from hunter.notify.security import get_owned_product
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_add_confirm_launch_194 import (
    handle_dialog_early_steps,
)


async def _handle_add_verification_failed(
    message: Message,
    chat_id: int,
    user_text: str,
    payload: dict[str, Any],
    db_inst: Any,
    current_user: User,
    start_verification_fn: Any,
) -> None:
    clean_url, _ = extract_avito_search_url(user_text)
    if clean_url:
        payload["search_url"] = clean_url
        await db_inst.save_pending_product(
            chat_id,
            "await_add_verify_retry",
            payload,
            ttl_hours=24,
            owner_user_id=current_user.id,
        )
        await start_verification_fn(message, payload, db_inst, current_user)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="add_verify_retry")],
            [InlineKeyboardButton(text="✏️ Изменить ссылку", callback_data="add_verify_edit_url")],
            [InlineKeyboardButton(text=ADD_CANCEL_BTN, callback_data="add_cancel")],
        ]
    )
    await message.reply(
        "Пожалуйста, нажмите «Попробовать снова», отправьте новую ссылку на поиск "
        "или нажмите «Отмена».",
        reply_markup=kb,
    )


async def handle_dialog_add_verification_steps(
    step: str,
    message: Message,
    chat_id: int,
    user_text: str,
    payload: dict[str, Any],
    db_inst: Any,
    current_user: User,
    start_verification_fn: Any,
) -> bool:
    if step == "await_add_reqs":
        payload["requirements"] = user_text[:300]
        await start_verification_fn(message, payload, db_inst, current_user)
        return True

    if step == "await_add_price_edit":
        try:
            price = parse_price_input(user_text)
        except ValueError:
            await message.reply(ADD_INVALID_PRICE)
            return True
        payload["price"] = price
        await start_verification_fn(message, payload, db_inst, current_user)
        return True

    if step == "await_add_url_edit":
        clean_url, err_msg = extract_avito_search_url(user_text)
        if not clean_url:
            await message.reply(err_msg or ADD_INVALID_URL)
            return True
        payload["search_url"] = clean_url
        await db_inst.save_pending_product(
            chat_id, "await_add_verify_retry", payload, ttl_hours=24, owner_user_id=current_user.id
        )
        await start_verification_fn(message, payload, db_inst, current_user)
        return True

    if step == "await_add_verify_failed":
        await _handle_add_verification_failed(
            message, chat_id, user_text, payload, db_inst, current_user, start_verification_fn
        )
        return True

    return False


async def handle_dialog_param_edit_steps(
    step: str,
    message: Message,
    chat_id: int,
    user_text: str,
    payload: dict[str, Any],
    db_inst: Any,
    current_user: User,
    store: Any,
    settings: Any,
    render_summary_fn: Any,
) -> bool:
    if step == "await_my_price":
        pid = payload.get("product_id")
        p = get_owned_product(current_user, pid, store)
        if not p:
            await message.reply(MY_PRODUCT_ALREADY_DELETED)
            await db_inst.delete_pending_product(chat_id)
            return True
        try:
            new_price = parse_price_input(user_text)
        except ValueError:
            await message.reply(ADD_INVALID_PRICE)
            return True
        p.max_price = new_price
        save_product_profile(
            p, directory=getattr(store, "directory", settings.data_dir / "products")
        )
        await db_inst.delete_pending_product(chat_id)
        price_str = f"{new_price:,}".replace(",", " ")
        await message.reply(MY_PRICE_UPDATED_TEMPLATE.format(name=p.name, price=price_str))
        return True

    if step == "await_edit_param":
        if "=" not in user_text:
            await message.reply(
                "Отправьте изменение в формате `поле=значение` (например: `price=40000`)."
            )
            return True
        k, v = user_text.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in ("price", "max_price"):
            payload["price"] = int(re.sub(r"\D", "", v))
        elif k in ("name", "title"):
            payload["name"] = v
        elif k in ("policy",):
            payload["policy"] = v
        await db_inst.save_pending_product(chat_id, "await_add_confirm", payload)
        await render_summary_fn(message, payload)
        return True

    return False


async def handle_dialog_support_text(
    message: Message,
    chat_id: int,
    user_text: str,
    db_inst: Any,
    current_user: User,
) -> None:
    if len(user_text) > 1000:
        await message.reply(SUPPORT_TOO_LONG)
        return
    await db_inst.delete_pending_product(chat_id)
    ticket_id = await db_inst.create_support_ticket(current_user.id, chat_id, user_text)
    await message.reply(SUPPORT_RECEIVED_TEMPLATE.format(ticket_id=ticket_id))
    owner = await db_inst.get_owner_user()
    if owner and owner.get("chat_id") and message.bot:
        user_repr = (
            f"@{current_user.username}" if current_user.username else f"ID {current_user.id}"
        )
        alert_text = SUPPORT_ALERT_OWNER_TEMPLATE.format(
            ticket_id=ticket_id, user_repr=user_repr, text=html.escape(user_text)
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=SUPPORT_BTN_REPLY, callback_data=f"ticket_reply:{ticket_id}"
                    )
                ]
            ]
        )
        try:
            await message.bot.send_message(
                chat_id=owner["chat_id"], text=alert_text, reply_markup=kb, parse_mode="HTML"
            )
        except Exception as exc:
            logger.warning("Failed notifying owner about support ticket #{}: {}", ticket_id, exc)


async def handle_dialog_ticket_reply(
    message: Message,
    chat_id: int,
    user_text: str,
    payload: dict[str, Any],
    db_inst: Any,
    current_user: User,
) -> None:
    if current_user.role != "owner":
        return
    if len(user_text) > 1000:
        await message.reply(SUPPORT_TOO_LONG)
        return
    ticket_id = int(payload.get("ticket_id", 0))
    await db_inst.delete_pending_product(chat_id)
    ticket = await db_inst.get_support_ticket(ticket_id)
    if not ticket:
        await message.reply("Обращение не найдено.")
        return
    target_user = await db_inst.get_user_by_id(ticket["user_id"])
    if not target_user or target_user.get("status") == "deleted":
        await db_inst.answer_support_ticket(
            ticket_id, current_user.id, user_text, status="undeliverable", error="user_deleted"
        )
        await message.reply(SUPPORT_DELIVERY_FAILED_DELETED)
        return
    try:
        user_msg = SUPPORT_USER_REPLY_TEMPLATE.format(
            ticket_id=ticket_id, text=html.escape(user_text)
        )
        await message.bot.send_message(chat_id=ticket["chat_id"], text=user_msg, parse_mode="HTML")
        await db_inst.answer_support_ticket(
            ticket_id, current_user.id, user_text, status="answered"
        )
        await message.reply(SUPPORT_REPLY_SUCCESS_TEMPLATE.format(ticket_id=ticket_id))
    except TelegramForbiddenError:
        await db_inst.mark_user_bot_blocked(target_user["id"])
        await db_inst.answer_support_ticket(
            ticket_id, current_user.id, user_text, status="undeliverable", error="bot_blocked"
        )
        await message.reply(SUPPORT_DELIVERY_FAILED_BLOCKED)
    except Exception as exc:
        await db_inst.answer_support_ticket(
            ticket_id, current_user.id, user_text, status="undeliverable", error=str(exc)
        )
        await message.reply(f"⚠️ Ошибка отправки ответа: {exc}")


async def handle_dialog_broadcast_step(
    step: str,
    message: Message,
    chat_id: int,
    user_text: str,
    db_inst: Any,
    current_user: User,
    broadcast_payloads: dict[int, str],
) -> bool:
    if step != "await_broadcast_text":
        return False
    if current_user.role != "owner":
        return True
    await db_inst.delete_pending_product(chat_id)
    broadcast_payloads[chat_id] = user_text
    counts = await get_broadcast_target_counts(db_inst)
    preview = BROADCAST_PREVIEW_TEMPLATE.format(
        text=html.escape(user_text),
        total=counts["all"],
        family=counts["family"],
        friend=counts["friend"],
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BROADCAST_BTN_ALL.format(count=counts["all"]), callback_data="bc:send:all"
                ),
                InlineKeyboardButton(
                    text=BROADCAST_BTN_FAMILY.format(count=counts["family"]),
                    callback_data="bc:send:family",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=BROADCAST_BTN_FRIEND.format(count=counts["friend"]),
                    callback_data="bc:send:friend",
                ),
                InlineKeyboardButton(text=BROADCAST_BTN_CANCEL, callback_data="bc:cancel"),
            ],
        ]
    )
    await message.reply(preview, reply_markup=kb, parse_mode="HTML")
    return True


async def _handle_dialog_early_waiting_steps(
    step: str,
    message: Message,
    chat_id: int,
    user_text: str,
    payload: dict[str, Any],
    db_inst: Any,
    current_user: User,
) -> bool:
    if await handle_dialog_early_steps(
        step, message, chat_id, user_text, payload, db_inst, current_user.id
    ):
        return True
    waiting_texts = {
        "await_add_policy": "выбора: именно эту модель или похожие",
        "await_add_photo": "фото товара или нажатия кнопки «Пропустить»",
    }
    expected = waiting_texts.get(step)
    if expected is None:
        return False
    await message.reply(ADD_WAITING_TEMPLATE.format(expected=expected))
    return True


async def handle_dialog_text_message(
    message: Message,
    current_user: User,
    db_inst: Any,
    store: Any,
    settings: Any,
    start_verification_fn: Any,
    render_summary_fn: Any,
    broadcast_payloads: dict[int, str],
) -> None:
    chat_id = message.chat.id
    state = await db_inst.get_pending_product(chat_id)
    if not state or (
        state.get("owner_user_id")
        and state["owner_user_id"] != current_user.id
        and current_user.role != "owner"
    ):
        return
    if await handle_public_add_message(
        message, db_inst, current_user, getattr(db_inst, "subscription_service", None)
    ):
        return

    step = state["step"]
    payload = state["payload"]
    user_text = (message.text or "").strip()

    if await _handle_dialog_early_waiting_steps(
        step, message, chat_id, user_text, payload, db_inst, current_user
    ):
        return
    if await handle_dialog_add_verification_steps(
        step, message, chat_id, user_text, payload, db_inst, current_user, start_verification_fn
    ):
        return
    if await handle_dialog_param_edit_steps(
        step,
        message,
        chat_id,
        user_text,
        payload,
        db_inst,
        current_user,
        store,
        settings,
        render_summary_fn,
    ):
        return
    if step == "await_support_text":
        await handle_dialog_support_text(message, chat_id, user_text, db_inst, current_user)
        return
    if step == "await_ticket_reply":
        await handle_dialog_ticket_reply(
            message, chat_id, user_text, payload, db_inst, current_user
        )
        return

    await handle_dialog_broadcast_step(
        step, message, chat_id, user_text, db_inst, current_user, broadcast_payloads
    )
