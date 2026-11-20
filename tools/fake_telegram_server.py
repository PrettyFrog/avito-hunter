"""Fake Telegram Bot API server and simulated user client (aiohttp, <= 400 lines).

Supports getMe, getUpdates, sendMessage, editMessageText, editMessageReplyMarkup,
answerCallbackQuery, deleteMessage, sendPhoto, sendMediaGroup, getFile, download,
setMyCommands, and fault injection (offline / status overrides).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
import time
from typing import Any
import uuid

from aiohttp import web


@dataclass
class TelegramMessage:
    message_id: int
    chat_id: int | str
    text: str
    date: int
    reply_markup: dict[str, Any] | None = None
    photo: list[dict[str, Any]] | None = None
    is_deleted: bool = False
    parse_mode: str | None = None

    def find_buttons(self) -> list[tuple[str, str]]:
        """Extract (text, callback_data) from inline keyboards."""
        buttons: list[tuple[str, str]] = []
        if not self.reply_markup or "inline_keyboard" not in self.reply_markup:
            return buttons
        for row in self.reply_markup["inline_keyboard"]:
            for btn in row:
                if "callback_data" in btn:
                    buttons.append((btn.get("text", ""), btn["callback_data"]))
        return buttons


def validate_telegram_html(text: str) -> None:
    allowed = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
               "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote"}
    for tag in re.findall(r"</?([a-zA-Z0-9_\-]+)", text):
        if tag.lower() not in allowed:
            raise ValueError(f"Disallowed HTML tag in Telegram message: <{tag}>")


def validate_markup(reply_markup: dict[str, Any] | None) -> None:
    if not reply_markup or "inline_keyboard" not in reply_markup:
        return
    for row in reply_markup["inline_keyboard"]:
        for btn in row:
            cb_data = btn.get("callback_data")
            if cb_data is not None and len(cb_data.encode("utf-8")) > 64:
                raise ValueError(f"callback_data exceeds 64 bytes: {cb_data!r}")


class FakeUser:
    """Simulated Telegram user for test stand interactions."""

    def __init__(self, stand: FakeTelegramStand, user_id: int, chat_id: int | None = None, username: str = "") -> None:
        self.stand = stand
        self.user_id = user_id
        self.chat_id = chat_id if chat_id is not None else user_id
        self.username = username or f"user_{user_id}"
        self.first_name = f"User_{user_id}"

    @property
    def messages(self) -> list[TelegramMessage]:
        return [m for m in self.stand.sent_messages if str(m.chat_id) == str(self.chat_id) and not m.is_deleted]

    @property
    def last_message(self) -> TelegramMessage | None:
        msgs = self.messages
        return msgs[-1] if msgs else None

    def _user_from(self) -> dict[str, Any]:
        return {"id": self.user_id, "is_bot": False, "first_name": self.first_name, "username": self.username}

    async def send_text(self, text: str) -> int:
        msg_id = self.stand.next_msg_id()
        await self.stand.enqueue_update({
            "update_id": self.stand.next_update_id(),
            "message": {"message_id": msg_id, "from": self._user_from(), "chat": {"id": self.chat_id, "type": "private"}, "date": int(time.time()), "text": text},
        })
        return msg_id

    async def send_command(self, cmd: str, args: str = "") -> int:
        return await self.send_text(f"/{cmd.lstrip('/')} {args}".strip())

    async def send_photo(self, file_id: str = "photo_default_123") -> int:
        msg_id = self.stand.next_msg_id()
        await self.stand.enqueue_update({
            "update_id": self.stand.next_update_id(),
            "message": {"message_id": msg_id, "from": self._user_from(), "chat": {"id": self.chat_id, "type": "private"}, "date": int(time.time()), "photo": [{"file_id": file_id, "file_unique_id": f"u_{file_id}", "width": 800, "height": 600}]},
        })
        return msg_id

    async def click(self, callback_data: str, message: TelegramMessage | None = None) -> str:
        tgt = message or self.last_message
        cb_id = f"cb_{uuid.uuid4().hex[:8]}"
        await self.stand.enqueue_update({
            "update_id": self.stand.next_update_id(),
            "callback_query": {
                "id": cb_id, "from": self._user_from(), "chat_instance": f"inst_{self.chat_id}", "data": callback_data,
                "message": {"message_id": tgt.message_id if tgt else 1, "chat": {"id": self.chat_id, "type": "private"}, "date": tgt.date if tgt else int(time.time()), "text": tgt.text if tgt else "", "reply_markup": tgt.reply_markup if tgt else {}},
            },
        })
        return cb_id

    async def wait_for_message(self, count_since_last: int = 1, timeout: float = 5.0) -> TelegramMessage:
        start_count = len(self.messages)
        start_t = time.monotonic()
        while time.monotonic() - start_t < timeout:
            if len(self.messages) >= start_count + count_since_last:
                return self.last_message
            await asyncio.sleep(0.05)
        raise TimeoutError(f"User {self.chat_id} timed out waiting for {count_since_last} new messages")

    async def wait_for_edit_or_message(self, timeout: float = 5.0) -> TelegramMessage:
        old_text = self.last_message.text if self.last_message else ""
        start_t = time.monotonic()
        while time.monotonic() - start_t < timeout:
            cur = self.last_message
            if cur and cur.text != old_text:
                return cur
            await asyncio.sleep(0.05)
        return self.last_message


class FakeTelegramStand:
    """Aiohttp mock Telegram Bot API server with fault injection support."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self.actual_port: int = 0
        self.bot_id = 987654321
        self.bot_username = "fake_avito_hunter_bot"
        self.offline = False
        self.status_override: int | None = None

        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._update_counter = 1000
        self._msg_counter = 5000
        self.updates_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent_messages: list[TelegramMessage] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.command_scopes: list[dict[str, Any]] = []

        self.app = web.Application(middlewares=[self._fault_middleware])
        self._setup_routes()

    @web.middleware
    async def _fault_middleware(self, request: web.Request, handler: Any) -> web.Response:
        if self.offline:
            return web.json_response({"ok": False, "error_code": 503, "description": "Service Unavailable"}, status=503)
        if self.status_override:
            return web.json_response(
                {"ok": False, "error_code": self.status_override, "description": "Fault injection error"},
                status=self.status_override,
            )
        return await handler(request)

    def next_update_id(self) -> int:
        self._update_counter += 1
        return self._update_counter

    def next_msg_id(self) -> int:
        self._msg_counter += 1
        return self._msg_counter

    async def enqueue_update(self, update: dict[str, Any]) -> None:
        await self.updates_queue.put(update)

    def get_user(self, user_id: int, chat_id: int | None = None, username: str = "") -> FakeUser:
        return FakeUser(self, user_id, chat_id=chat_id, username=username)

    def _setup_routes(self) -> None:
        r = self.app.router
        r.add_route("*", "/bot{token}/getMe", self._handle_get_me)
        r.add_route("*", "/bot{token}/deleteWebhook", self._handle_delete_webhook)
        r.add_route("*", "/bot{token}/setMyCommands", self._handle_set_my_commands)
        r.add_route("*", "/bot{token}/getUpdates", self._handle_get_updates)
        r.add_route("POST", "/bot{token}/sendMessage", self._handle_send_message)
        r.add_route("POST", "/bot{token}/editMessageText", self._handle_edit_message_text)
        r.add_route("POST", "/bot{token}/editMessageReplyMarkup", self._handle_edit_markup)
        r.add_route("POST", "/bot{token}/answerCallbackQuery", self._handle_answer_callback)
        r.add_route("POST", "/bot{token}/deleteMessage", self._handle_delete_message)
        r.add_route("POST", "/bot{token}/sendPhoto", self._handle_send_photo)
        r.add_route("POST", "/bot{token}/sendMediaGroup", self._handle_send_media_group)
        r.add_route("*", "/bot{token}/getFile", self._handle_get_file)
        r.add_route("GET", "/file/bot{token}/{file_path:.*}", self._handle_download_file)

    async def _parse_req(self, request: web.Request) -> dict[str, Any]:
        if request.content_type == "application/json":
            return await request.json()
        if request.content_type.startswith("multipart/form-data"):
            reader = await request.multipart()
            data: dict[str, Any] = {}
            while (part := await reader.next()) is not None:
                raw = await part.read()
                if part.filename:
                    data[part.name] = raw
                else:
                    text_val = raw.decode("utf-8")
                    try:
                        data[part.name] = json.loads(text_val)
                    except Exception:
                        data[part.name] = text_val
            return data
        post_data = await request.post()
        res: dict[str, Any] = {}
        for k, v in post_data.items():
            if isinstance(v, str) and (v.startswith("{") or v.startswith("[")):
                try:
                    res[k] = json.loads(v)
                    continue
                except Exception:
                    pass
            res[k] = v
        return res

    async def _handle_get_me(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "result": {
                "id": self.bot_id, "is_bot": True, "first_name": "AvitoHunterBot",
                "username": self.bot_username, "can_join_groups": False,
                "can_read_all_group_messages": False, "supports_inline_queries": False,
            },
        })

    async def _handle_delete_webhook(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "result": True})

    async def _handle_set_my_commands(self, request: web.Request) -> web.Response:
        payload = await self._parse_req(request)
        self.command_scopes.append(payload)
        return web.json_response({"ok": True, "result": True})

    async def _handle_get_updates(self, request: web.Request) -> web.Response:
        payload = await self._parse_req(request)
        timeout = float(payload.get("timeout", request.query.get("timeout", 0.0)))
        offset = int(payload.get("offset", request.query.get("offset", 0)))
        results: list[dict[str, Any]] = []

        while not self.updates_queue.empty():
            upd = self.updates_queue.get_nowait()
            if upd.get("update_id", 0) >= offset:
                results.append(upd)
        if not results and timeout > 0:
            try:
                upd = await asyncio.wait_for(self.updates_queue.get(), timeout=min(timeout, 1.5))
                if upd.get("update_id", 0) >= offset:
                    results.append(upd)
            except asyncio.TimeoutError:
                pass
        return web.json_response({"ok": True, "result": results})

    def _build_msg_dict(self, msg: TelegramMessage) -> dict[str, Any]:
        d: dict[str, Any] = {
            "message_id": msg.message_id,
            "from": {"id": self.bot_id, "is_bot": True, "first_name": "AvitoHunterBot", "username": self.bot_username},
            "chat": {"id": int(msg.chat_id) if str(msg.chat_id).isdigit() else 1, "type": "private"},
            "date": msg.date,
            "text": msg.text,
        }
        if msg.reply_markup:
            d["reply_markup"] = msg.reply_markup
        if msg.photo:
            d["photo"] = msg.photo
        return d

    def _validate_content(self, data: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        text = str(data.get("text", ""))
        if len(text) > 4096:
            raise web.HTTPBadRequest(text="Message is too long")
        if data.get("parse_mode") == "HTML":
            validate_telegram_html(text)
        markup = data.get("reply_markup")
        if isinstance(markup, str):
            markup = json.loads(markup)
        validate_markup(markup)
        return text, markup

    async def _handle_send_message(self, request: web.Request) -> web.Response:
        data = await self._parse_req(request)
        text, markup = self._validate_content(data)
        msg = TelegramMessage(
            message_id=self.next_msg_id(), chat_id=data.get("chat_id", 0),
            text=text, date=int(time.time()), reply_markup=markup,
            parse_mode=data.get("parse_mode"),
        )
        self.sent_messages.append(msg)
        return web.json_response({"ok": True, "result": self._build_msg_dict(msg)})

    async def _handle_edit_message_text(self, request: web.Request) -> web.Response:
        data = await self._parse_req(request)
        msg_id = int(data.get("message_id", 0))
        text, markup = self._validate_content(data)
        target = next((m for m in self.sent_messages if m.message_id == msg_id), None)
        if target:
            target.text = text
            if markup is not None:
                target.reply_markup = markup
            return web.json_response({"ok": True, "result": self._build_msg_dict(target)})
        mock_msg = TelegramMessage(msg_id, data.get("chat_id", 0), text, int(time.time()), reply_markup=markup)
        self.sent_messages.append(mock_msg)
        return web.json_response({"ok": True, "result": self._build_msg_dict(mock_msg)})

    async def _handle_edit_markup(self, request: web.Request) -> web.Response:
        data = await self._parse_req(request)
        msg_id = int(data.get("message_id", 0))
        markup = data.get("reply_markup")
        if isinstance(markup, str):
            markup = json.loads(markup)
        validate_markup(markup)
        target = next((m for m in self.sent_messages if m.message_id == msg_id), None)
        if target:
            target.reply_markup = markup
            return web.json_response({"ok": True, "result": self._build_msg_dict(target)})
        return web.json_response({"ok": True, "result": True})

    async def _handle_answer_callback(self, request: web.Request) -> web.Response:
        self.answered_callbacks.append(await self._parse_req(request))
        return web.json_response({"ok": True, "result": True})

    async def _handle_delete_message(self, request: web.Request) -> web.Response:
        data = await self._parse_req(request)
        msg_id = int(data.get("message_id", 0))
        target = next((m for m in self.sent_messages if m.message_id == msg_id), None)
        if target:
            target.is_deleted = True
        return web.json_response({"ok": True, "result": True})

    async def _handle_send_photo(self, request: web.Request) -> web.Response:
        data = await self._parse_req(request)
        markup = data.get("reply_markup")
        if isinstance(markup, str):
            markup = json.loads(markup)
        validate_markup(markup)
        msg = TelegramMessage(
            message_id=self.next_msg_id(), chat_id=data.get("chat_id", 0),
            text=str(data.get("caption", "")), date=int(time.time()),
            reply_markup=markup, photo=[{"file_id": "file_photo_1", "width": 800, "height": 600}],
        )
        self.sent_messages.append(msg)
        return web.json_response({"ok": True, "result": self._build_msg_dict(msg)})

    async def _handle_send_media_group(self, request: web.Request) -> web.Response:
        data = await self._parse_req(request)
        media_list = data.get("media", [])
        if isinstance(media_list, str):
            media_list = json.loads(media_list)
        results = [
            self._build_msg_dict(TelegramMessage(
                message_id=self.next_msg_id(), chat_id=data.get("chat_id", 0),
                text=item.get("caption", ""), date=int(time.time()),
                photo=[{"file_id": "photo_item_id", "width": 800, "height": 600}],
            ))
            for item in media_list
        ]
        return web.json_response({"ok": True, "result": results})

    async def _handle_get_file(self, request: web.Request) -> web.Response:
        data = await self._parse_req(request)
        fid = str(data.get("file_id", "photo_default"))
        return web.json_response({"ok": True, "result": {"file_id": fid, "file_path": f"photos/{fid}.jpg", "file_size": 128}})

    async def _handle_download_file(self, request: web.Request) -> web.Response:
        dummy_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        return web.Response(body=dummy_jpeg, content_type="image/jpeg")

    async def start(self) -> str:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        sockets = self._site._server.sockets
        self.actual_port = sockets[0].getsockname()[1] if sockets else self.port
        return f"http://{self.host}:{self.actual_port}"

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
