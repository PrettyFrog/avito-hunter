"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_on_ref_confirm(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]
    owner_chat_id = outer["owner_chat_id"]

    @router.callback_query(_router_globals.F.data.startswith("ref_confirm:"))
    @_router_globals.requires("owner")
    async def on_ref_confirm(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_ref_confirm_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(),
            owner_chat_id,
            store=_router_globals.ProductStore(),
            settings=_router_globals.get_settings(),
        )


def register_on_ref_cancel(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    owner_chat_id = outer["owner_chat_id"]

    @router.callback_query(_router_globals.F.data.startswith("ref_cancel"))
    @_router_globals.requires("owner")
    async def on_ref_cancel(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_ref_cancel_callback(query, _resolve_db(), owner_chat_id)


def register_on_feedback(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("fb:"))
    @_router_globals.requires("user")
    async def on_feedback(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_feedback_callback(
            query, _resolve_user(current_user), _resolve_db(db), _router_globals._LABELS
        )


def register_cb_alert_pause_prod(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("pause_prod:"))
    @_router_globals.requires("user")
    async def cb_alert_pause_prod(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_alert_pause_prod_callback(
            query, _resolve_user(current_user), _resolve_store(), _router_globals.get_settings()
        )


def register_on_audit_feedback(router: Router, outer: dict[str, Any]) -> None:
    @router.callback_query(_router_globals.F.data.startswith("audit:"))
    @_router_globals.requires("owner")
    async def on_audit_feedback(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_audit_feedback_callback(query, db=_router_globals.get_db())


def register_on_add_confirm(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("add_ok:"))
    @_router_globals.requires("owner")
    async def on_add_confirm(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_confirm_legacy_callback(
            query,
            _resolve_user(current_user),
            db=_router_globals.get_db(),
            settings=_router_globals.get_settings(),
        )


def register_on_toggle_archive(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_toggle_archive")
    @_router_globals.requires("owner")
    async def on_toggle_archive(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_toggle_archive_callback(
            query,
            _resolve_user(current_user),
            _router_globals._render_summary_card,
            db=_router_globals.get_db(),
        )


def register_on_add_cancel(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_cancel")
    @_router_globals.requires("user")
    async def on_add_cancel(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_cancel_callback(
            query, _resolve_user(current_user), _resolve_db()
        )


def register_on_add_edit(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_edit")
    @_router_globals.requires("owner")
    async def on_add_edit(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_edit_callback(
            query, _resolve_user(current_user), db=_router_globals.get_db()
        )


def register_on_my_chat_member(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    channel = outer["channel"]
    owner_chat_id = outer["owner_chat_id"]

    @router.my_chat_member()
    @_router_globals.requires("user")
    async def on_my_chat_member(
        event: _router_globals.ChatMemberUpdated,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_my_chat_member_event(
            event,
            current_user,
            _resolve_db(db),
            owner_chat_id,
            channel,
            store=_router_globals.get_product_store(),
            settings=_router_globals.get_settings(),
        )


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_on_ref_confirm(router, outer)
    register_on_ref_cancel(router, outer)
    register_on_feedback(router, outer)
    register_cb_alert_pause_prod(router, outer)
    register_on_audit_feedback(router, outer)
    register_on_add_confirm(router, outer)
    register_on_toggle_archive(router, outer)
    register_on_add_cancel(router, outer)
    register_on_add_edit(router, outer)
    register_on_my_chat_member(router, outer)
