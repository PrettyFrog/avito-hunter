"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cb_stale_del(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("stale_del:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_stale_del(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_stale_del_callback(
            callback, _resolve_user(current_user), _resolve_store()
        )


def register_on_dialog_text(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.F.text & ~_router_globals.F.text.startswith("/"))
    @_router_globals.requires("user")
    async def on_dialog_text(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_dialog_text_message(
            message,
            _resolve_user(current_user),
            _resolve_db(),
            _router_globals.get_product_store(),
            _router_globals.get_settings(),
            _router_globals._start_add_verification_user,
            _router_globals._render_summary_card,
            _router_globals._BROADCAST_PAYLOADS,
        )


def register_on_photo_attachment(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.F.photo | _router_globals.F.document)
    @_router_globals.requires("user")
    async def on_photo_attachment(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_photo_attachment_message(
            message, _resolve_user(current_user), _resolve_db()
        )


def register_on_alert_ref_add(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("ref_add:"))
    @_router_globals.requires("owner")
    async def on_alert_ref_add(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_alert_ref_add_callback(
            query,
            _resolve_user(current_user),
            store=_router_globals.ProductStore(),
            settings=_router_globals.get_settings(),
        )


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cb_stale_del(router, outer)
    register_on_dialog_text(router, outer)
    register_on_photo_attachment(router, outer)
    register_on_alert_ref_add(router, outer)
