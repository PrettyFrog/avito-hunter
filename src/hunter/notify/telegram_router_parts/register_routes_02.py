"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cb_faq_menu(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "faq_menu")
    @_router_globals.requires("user")
    async def cb_faq_menu(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        _resolve_user(current_user)
        await _router_globals.handle_faq_menu_callback(callback)


def register_cb_faq_item(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("faq:"))
    @_router_globals.requires("user")
    async def cb_faq_item(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        _resolve_user(current_user)
        await _router_globals.handle_faq_item_callback(callback)


def register_cmd_quiet(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("quiet"))
    @_router_globals.requires("user")
    async def cmd_quiet(
        message: _router_globals.Message,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        current_user = _resolve_user(current_user)
        database = _resolve_db(db)
        await _router_globals.handle_quiet_command(message, current_user, database)


def register_cmd_guide(router: Router, outer: dict[str, Any]) -> None:
    @router.message(_router_globals.Command("guide"))
    @_router_globals.requires("user")
    async def cmd_guide(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_guide_command(message, _router_globals._safe_reply)


def register_cmd_support(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("support"))
    @_router_globals.requires("user")
    async def cmd_support(
        message: _router_globals.Message,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db(db)
        await _router_globals.handle_support_command(
            message, current_user, db_inst, _router_globals._safe_reply
        )


def register_cmd_tickets(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]

    @router.message(_router_globals.Command("tickets"))
    @_router_globals.requires("owner")
    async def cmd_tickets(
        message: _router_globals.Message,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        db_inst = _resolve_db(db)
        await _router_globals.handle_tickets_command(message, db_inst, _router_globals._safe_reply)


def register_cb_ticket_reply(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("ticket_reply:"))
    @_router_globals.requires("owner")
    async def cb_ticket_reply(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db(db)
        await _router_globals.handle_ticket_reply_callback(query, current_user, db_inst)


def register_cmd_broadcast(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]

    @router.message(_router_globals.Command("broadcast"))
    @_router_globals.requires("owner")
    async def cmd_broadcast(
        message: _router_globals.Message,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        db_inst = _resolve_db(db)
        await _router_globals.handle_broadcast_command(
            message, db_inst, _router_globals._safe_reply
        )


def register_cb_broadcast_send(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]
    channel = outer["channel"]

    @router.callback_query(_router_globals.F.data.startswith("bc:send:"))
    @_router_globals.requires("owner")
    async def cb_broadcast_send(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db(db)
        default_bot = getattr(channel, "bot", None)
        await _router_globals.handle_broadcast_send_callback(
            query, current_user, db_inst, default_bot
        )


def register_cb_broadcast_cancel(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "bc:cancel")
    @_router_globals.requires("owner")
    async def cb_broadcast_cancel(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db(db)
        await _router_globals.handle_broadcast_cancel_callback(query, current_user, db_inst)


def register_cmd_delete_me(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("delete_me"))
    @_router_globals.requires("user")
    async def cmd_delete_me(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        current_user = _resolve_user(current_user)
        await _router_globals.handle_delete_me_command(
            message, current_user, _router_globals._safe_reply
        )


def register_cb_delme_confirm(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "delme:confirm")
    @_router_globals.requires("user")
    async def cb_delme_confirm(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db(db)
        store = _resolve_store()
        await _router_globals.handle_delete_me_confirm_callback(query, current_user, db_inst, store)


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cb_faq_menu(router, outer)
    register_cb_faq_item(router, outer)
    register_cmd_quiet(router, outer)
    register_cmd_guide(router, outer)
    register_cmd_support(router, outer)
    register_cmd_tickets(router, outer)
    register_cb_ticket_reply(router, outer)
    register_cmd_broadcast(router, outer)
    register_cb_broadcast_send(router, outer)
    register_cb_broadcast_cancel(router, outer)
    register_cmd_delete_me(router, outer)
    register_cb_delme_confirm(router, outer)
