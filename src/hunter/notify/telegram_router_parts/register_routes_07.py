"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations
from typing import Any
from aiogram import Router
from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cb_admin_errors(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]

    @router.callback_query(_router_globals.F.data.startswith("admin:errors"))
    @_router_globals.requires("owner")
    async def cb_admin_errors(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_errors_callback(
            query, _resolve_db(db), _router_globals._safe_edit_text
        )


def register_cmd_errors(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]

    @router.message(_router_globals.Command("errors", "errs"))
    @_router_globals.requires("owner")
    async def cmd_errors(
        message: _router_globals.Message,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_errors_command(
            message, _resolve_db(db), _router_globals._safe_reply
        )


def register_cmd_settings(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("settings"))
    @_router_globals.requires("user")
    async def cmd_settings(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_settings_command(
            message, _resolve_user(current_user), _router_globals._safe_reply
        )


def register_cmd_my(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("my"))
    @router.message(_router_globals.F.text == "📋 Мои товары")
    @_router_globals.requires("user")
    async def cmd_my(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_command(
            message,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._format_product_card,
            _router_globals._product_card_keyboard,
        )


def register_cb_my_pause(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_pause:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_pause(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_pause_callback(
            callback,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._format_product_card,
            _router_globals._product_card_keyboard,
        )


def register_cb_my_resume(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_resume:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_resume(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_resume_callback(
            callback,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._format_product_card,
            _router_globals._product_card_keyboard,
        )


def register_cb_my_price(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_price:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_price(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_price_callback(
            callback, _resolve_user(current_user), _resolve_store(), _resolve_db()
        )


def register_cb_my_photo(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_photo:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_photo(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_photo_callback(
            callback, _resolve_user(current_user), _resolve_store(), _resolve_db()
        )


def register_cb_my_findings(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_findings:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_findings(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_findings_callback(
            callback,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._get_findings_for_product,
        )


def register_cb_my_del_ask(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_del_ask:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_del_ask(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_del_ask_callback(
            callback, _resolve_user(current_user), _resolve_store()
        )


def register_cb_my_del_cancel(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_del_cancel:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_del_cancel(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_del_cancel_callback(
            callback,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._format_product_card,
            _router_globals._product_card_keyboard,
        )


def register_cb_my_del_do(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("my_del_do:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_my_del_do(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_my_del_do_callback(
            callback, _resolve_user(current_user), _resolve_store()
        )


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cb_admin_errors(router, outer)
    register_cmd_errors(router, outer)
    register_cmd_settings(router, outer)
    register_cmd_my(router, outer)
    register_cb_my_pause(router, outer)
    register_cb_my_resume(router, outer)
    register_cb_my_price(router, outer)
    register_cb_my_photo(router, outer)
    register_cb_my_findings(router, outer)
    register_cb_my_del_ask(router, outer)
    register_cb_my_del_cancel(router, outer)
    register_cb_my_del_do(router, outer)
