from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import time
from typing import Any

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BotCommand, InlineKeyboardMarkup
from loguru import logger

from hunter.core.config import get_settings
from hunter.core.models import NotificationEvent
from hunter.notify.base import Channel, format_event
from hunter.notify.telegram_parts.telegramchannel_send_01 import (
    dispatch_telegram_envelope,
    prepare_notification_payload,
    resolve_photo_input,
)
from hunter.transport.telegram_relay import (
    TelegramRelayClient,
    TelegramRelayConfig,
    TelegramRelayError,
)

__all__ = [
    "OWNER_BOT_COMMANDS",
    "TelegramChannel",
    "USER_BOT_COMMANDS",
    "format_user_alert",
    "setup_bot_commands",
    "user_alert_keyboard",
]
from hunter.notify.messages_ru import (
    ALERT_BTN_BAD,
    ALERT_BTN_GOOD,
    ALERT_BTN_OPEN,
    ALERT_BTN_PAUSE_PROD,
    ALERT_REASON_PHOTO,
    ALERT_REASON_TITLE,
    ALERT_USER_CANDIDATE_HEADER,
    ALERT_USER_CANDIDATE_REASON,
)

_LABELS = {
    "ok": "👍 норм",
    "fake": "🚫 фейк",
    "expensive": "📈 дорого",
    "wrong_model": "❌ не та модель",
}


class TelegramRelaySession(BaseSession):
    """Aiogram session that forwards API methods through the configured relay."""

    def __init__(self, relay: TelegramRelayClient) -> None:
        super().__init__(timeout=relay.config.timeout_seconds)
        self.relay = relay

    async def make_request(self, bot: Any, method: Any, timeout: int | None = None) -> Any:
        files: dict[str, Any] = {}
        parameters = {
            name: prepared
            for name, value in method.model_dump(warnings=False).items()
            if (prepared := self.prepare_value(value, bot=bot, files=files)) is not None
        }
        if files:
            raise TelegramBadRequest(
                method=method, message="Relay does not support file uploads"
            )
        try:
            async with asyncio.timeout(timeout if timeout is not None else self.timeout):
                result = await self.relay.call(method.__api_method__, bot.token, parameters)
        except TimeoutError:
            raise TelegramNetworkError(
                method=method, message="Telegram relay request timed out"
            ) from None
        except TelegramRelayError as exc:
            raise TelegramNetworkError(method=method, message=str(exc)) from None
        status_code = result.get("error_code", 200)
        if not isinstance(status_code, int):
            raise TelegramNetworkError(
                method=method, message="Telegram relay returned invalid status"
            )
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=status_code,
            content=self.json_dumps(result),
        )
        return response.result

    async def close(self) -> None:
        await self.relay.close()

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        if False:
            yield b""
        raise TelegramRelayError(
            "Telegram file downloads are unavailable through the JSON relay"
        )


def format_user_alert(event: NotificationEvent) -> str:
    """Formats friendly, jargon-free notification text for regular users (M3)."""
    from hunter.core.product import get_product_store

    store = get_product_store()
    profile = store.get(event.watchlist_id) if event.watchlist_id else None

    # Check if candidate / loose match
    is_candidate = (event.verdict == "candidate") or any("кандидат" in r.lower() for r in (event.reasons or []))

    max_price = profile.max_price if profile else None
    price_val = f"{event.price:,} ₽".replace(",", " ") if event.price else "цена не указана"
    max_price_val = f"{max_price:,} ₽".replace(",", " ") if max_price else "—"

    time_ago = getattr(event, "age_text", None) or "только что"
    city = getattr(event, "city", None) or "Авито"
    from hunter.notify.helpers import format_publication_delay
    delay_str = format_publication_delay(getattr(event, "published_at", None))

    if getattr(event, "price_reduced_from_above_max", None):
        old_p, _ = event.price_reduced_from_above_max
        old_p_val = f"{old_p:,} ₽".replace(",", " ")
        header = f"📉 Цена снизилась: {event.title}"
        by_photo = any("по фото" in r.lower() for r in (event.reasons or []))
        reason = ALERT_REASON_PHOTO if by_photo else ALERT_REASON_TITLE
        price_line = f"💰 {price_val} (было {old_p_val}, ваш максимум {max_price_val})"
    elif is_candidate:
        header = f"{ALERT_USER_CANDIDATE_HEADER}: {event.title}"
        reason = ALERT_USER_CANDIDATE_REASON
        price_line = f"💰 {price_val} (ваш максимум {max_price_val})"
    else:
        header = f"🎧 Нашёл: {event.title}"
        by_photo = any("по фото" in r.lower() for r in (event.reasons or []))
        reason = ALERT_REASON_PHOTO if by_photo else ALERT_REASON_TITLE
        price_line = f"💰 {price_val} (ваш максимум {max_price_val})"

    lines = [
        header,
        price_line,
        f"📍 {city} · {time_ago}",
    ]
    if hasattr(event, "specs") and event.specs:
        bh = event.specs.get("battery_health")
        cyc = event.specs.get("battery_cycles")
        if bh:
            if event.specs.get("battery_degraded"):
                lines.append(f"⚠️ Износ АКБ: {bh}" + (f" ({cyc} ц.)" if cyc else ""))
            else:
                lines.append(f"🔋 АКБ: {bh}" + (f" ({cyc} ц.)" if cyc else ""))
    if delay_str:
        lines.append(f"⏱ Задержка от публикации: {delay_str}")
    lines.append(reason)
    return "\n".join(lines)


def user_alert_keyboard(
    avito_id: str, url: str | None = None, product_id: str | None = None
) -> InlineKeyboardMarkup:
    """Builds simple inline keyboard for regular users (M3)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    if url:
        rows.append([InlineKeyboardButton(text="🔗 " + ALERT_BTN_OPEN, url=url)])
    rows.append([
        InlineKeyboardButton(text=ALERT_BTN_GOOD, callback_data=f"fb:{avito_id}:target_model"),
        InlineKeyboardButton(text=ALERT_BTN_BAD, callback_data=f"fb:{avito_id}:wrong_model"),
    ])
    if product_id:
        rows.append([
            InlineKeyboardButton(text=ALERT_BTN_PAUSE_PROD, callback_data=f"pause_prod:{product_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, settings: Any = None) -> None:
        settings = settings or get_settings()
        self.settings = settings
        token = settings.telegram_bot_token
        self.token = token.get_secret_value() if hasattr(token, "get_secret_value") else token
        self.chat_id = settings.telegram_chat_id
        self._bot = None
        self.last_delivery_result = None
        self._relay_config = None
        self._secondary_relay_config = None
        public_v2 = getattr(settings, "public_v2", None)
        if getattr(public_v2, "enabled", False) is True:
            egress = getattr(settings, "telegram_egress", None)
            require_relay = getattr(public_v2, "require_telegram_relay", True)
            if (
                require_relay
                and not getattr(settings, "telegram_api_base", None)
                and (not egress or not egress.primary_endpoint or not egress.hmac_secret)
            ):
                raise RuntimeError("Public v2 requires a configured Telegram HTTPS relay")
            if egress and egress.primary_endpoint and egress.hmac_secret:
                self._relay_config = TelegramRelayConfig(
                    endpoint=egress.primary_endpoint,
                    allowed_hosts=frozenset(egress.allowed_hosts),
                    hmac_secret=egress.hmac_secret,
                    timeout_seconds=egress.timeout_seconds,
                )
                if egress.secondary_endpoint:
                    self._secondary_relay_config = TelegramRelayConfig(
                        endpoint=egress.secondary_endpoint,
                        allowed_hosts=frozenset(egress.allowed_hosts),
                        hmac_secret=egress.hmac_secret,
                        timeout_seconds=egress.timeout_seconds,
                    )

    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _client(self):
        if self._bot is None:
            from aiogram import Bot
            from aiogram.client.session.aiohttp import AiohttpSession

            settings = self.settings
            if getattr(settings, "telegram_api_base", None):
                from aiogram.client.telegram import TelegramAPIServer

                session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_base))
                session._connector_init["keepalive_timeout"] = 60.0
                self._bot = Bot(token=self.token, session=session)
            elif self._relay_config:
                relay = TelegramRelayClient(
                    self._relay_config, secondary_config=self._secondary_relay_config
                )
                self._bot = Bot(token=self.token, session=TelegramRelaySession(relay))
            else:
                session = AiohttpSession(proxy=settings.http_proxy) if settings.http_proxy else AiohttpSession()
                session._connector_init["keepalive_timeout"] = 60.0
                self._bot = Bot(token=self.token, session=session)
        return self._bot

    def _keyboard(self, avito_id: str, url: str | None = None, product_id: str | None = None):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        buttons = []
        if url:
            buttons.append([InlineKeyboardButton(text="🔗 Открыть на Авито", url=url)])
        buttons.extend([
            [
                InlineKeyboardButton(text="✅ та модель", callback_data=f"fb:{avito_id}:target_model"),
                InlineKeyboardButton(text="❌ не та", callback_data=f"fb:{avito_id}:wrong_model"),
            ],
            [
                InlineKeyboardButton(text="📌 эталон", callback_data=f"ref_add:{avito_id}:{product_id or ''}"),
                InlineKeyboardButton(text=_LABELS["ok"], callback_data=f"fb:{avito_id}:ok"),
            ],
        ])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    async def send(self, event: NotificationEvent) -> bool:
        if not self.enabled():
            return False
        bot = await self._client()
        target_chat_id = event.recipient_chat_id or self.chat_id
        text, markup = prepare_notification_payload(
            self,
            event,
            target_chat_id,
            format_user_alert_fn=format_user_alert,
            user_alert_keyboard_fn=user_alert_keyboard,
            format_event_fn=format_event,
        )
        settings = get_settings()
        photo_input = await resolve_photo_input(event, settings)
        return await dispatch_telegram_envelope(
            channel=self,
            bot=bot,
            event=event,
            target_chat_id=target_chat_id,
            text=text,
            markup=markup,
            photo_input=photo_input,
            settings=settings,
        )

    async def alert(self, text: str) -> bool:
        if not self.enabled():
            logger.warning("telegram not configured, alert: {}", text)
            return False
        if getattr(self, "_alert_cooldown_until", 0) > time.monotonic():
            return False
        bot = await self._client()
        try:
            if len(text) <= 4000:
                await bot.send_message(self.chat_id, text)
            else:
                for i in range(0, len(text), 4000):
                    await bot.send_message(self.chat_id, text[i : i + 4000])
            return True
        except TelegramRetryAfter as exc:
            self._alert_cooldown_until = time.monotonic() + exc.retry_after
            logger.error("telegram alert flood limited, cooling down for {}s", exc.retry_after)
            return False
        except Exception as exc:
            logger.error(
                "telegram alert failed: {}. Make sure you sent /start to your bot in Telegram!", exc
            )
            return False

    async def listen(self, db: Any = None, subscription_service: Any = None) -> None:
        if not self.enabled():
            return
        import asyncio

        from aiogram import Dispatcher

        from hunter.notify.telegram_router import setup_telegram_router

        dp = Dispatcher()
        self._dp = dp
        router = setup_telegram_router(self, db=db, subscription_service=subscription_service)
        dp.include_router(router)

        bot = await self._client()
        await setup_bot_commands(bot, self.chat_id)
        logger.info("telegram polling with full PR-3.5 command router started")
        try:
            await dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query", "my_chat_member"],
                handle_signals=False,
                close_bot_session=True,
            )
        except asyncio.CancelledError:
            pass

    async def stop_listener(self) -> None:
        if getattr(self, "_dp", None) is not None:
            await self._dp.stop_polling()


USER_BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск и главное меню"),
    BotCommand(command="help", description="Справка по всем командам"),
    BotCommand(command="add", description="Добавить товар для поиска"),
    BotCommand(command="bind_vk", description="Привязать личные сообщения VK"),
    BotCommand(command="my", description="Мои товары"),
]

OWNER_BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск и главное меню"),
    BotCommand(command="help", description="Справка по всем командам"),
    BotCommand(command="add", description="Добавить товар для поиска"),
    BotCommand(command="bind_vk", description="Привязать личные сообщения VK"),
    BotCommand(command="my", description="Мои товары"),
]


async def setup_bot_commands(bot: Any, owner_chat_id: int | str | None = None) -> None:
    try:
        from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat

        await bot.set_my_commands(
            commands=USER_BOT_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        if owner_chat_id:
            try:
                owner_id_int = int(owner_chat_id)
                await bot.set_my_commands(
                    commands=OWNER_BOT_COMMANDS,
                    scope=BotCommandScopeChat(chat_id=owner_id_int),
                )
            except (ValueError, TypeError) as e:
                logger.debug("Could not parse owner_chat_id for custom scope: {}", e)
    except Exception as e:
        logger.warning("Failed to setup bot commands menu: {}", e)
