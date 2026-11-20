"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cmd_add(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("add"))
    @router.message(_router_globals.F.text == "➕ Добавить товар")
    @_router_globals.requires("user")
    async def cmd_add(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_command(
            message,
            _resolve_user(current_user),
            _resolve_db(),
            _router_globals.get_product_store(),
            _router_globals._start_add_verification,
        )


def register_cb_start_add(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "btn_start_add")
    @_router_globals.requires("user")
    async def cb_start_add(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_start_add_callback(
            callback,
            _resolve_user(current_user),
            _router_globals.get_product_store(),
            _resolve_db(),
        )


def register_cb_add_faq_url(router: Router, outer: dict[str, Any]) -> None:
    @router.callback_query(_router_globals.F.data == "add_faq_url")
    @_router_globals.requires("user")
    async def cb_add_faq_url(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_faq_url_callback(callback)


def register_cb_add_policy(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("add_policy:"))
    @_router_globals.requires("user")
    async def cb_add_policy(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_policy_callback(
            callback, _resolve_user(current_user), _resolve_db()
        )


def register_cb_add_photo_proceed(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.in_(("add_photo_skip", "add_photo_done")))
    @_router_globals.requires("user")
    async def cb_add_photo_proceed(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_photo_proceed_callback(
            callback, _resolve_user(current_user), _resolve_db()
        )


def register_cb_add_reqs_skip(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_reqs_skip")
    @_router_globals.requires("user")
    async def cb_add_reqs_skip(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_reqs_skip_callback(
            callback,
            _resolve_user(current_user),
            _resolve_db(),
            _router_globals._start_add_verification_user,
        )


def register_cb_add_verify_retry(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_verify_retry")
    @_router_globals.requires("user")
    async def cb_add_verify_retry(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_verify_retry_callback(
            callback,
            _resolve_user(current_user),
            _resolve_db(),
            _router_globals._start_add_verification_user,
        )


def register_cb_add_verify_edit_url(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_verify_edit_url")
    @_router_globals.requires("user")
    async def cb_add_verify_edit_url(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_verify_edit_url_callback(
            callback, _resolve_user(current_user), _resolve_db()
        )


def register_cb_add_edit_price(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_edit_price")
    @_router_globals.requires("user")
    async def cb_add_edit_price(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_edit_price_callback(
            callback, _resolve_user(current_user), _resolve_db()
        )


def register_cb_add_confirm_launch(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "add_confirm_launch")
    @_router_globals.requires("user")
    async def cb_add_confirm_launch(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_add_confirm_launch_callback(
            callback, _resolve_user(current_user), _resolve_db(), _resolve_store()
        )


def register_cb_stale_keep(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("stale_keep:"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cb_stale_keep(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_stale_keep_callback(
            callback, _resolve_user(current_user), _resolve_store()
        )


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cmd_add(router, outer)
    register_cb_start_add(router, outer)
    register_cb_add_faq_url(router, outer)
    register_cb_add_policy(router, outer)
    register_cb_add_photo_proceed(router, outer)
    register_cb_add_reqs_skip(router, outer)
    register_cb_add_verify_retry(router, outer)
    register_cb_add_verify_edit_url(router, outer)
    register_cb_add_edit_price(router, outer)
    register_cb_add_confirm_launch(router, outer)
    register_cb_stale_keep(router, outer)
