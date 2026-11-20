"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cmd_found(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("found"))
    @_router_globals.requires("user")
    async def cmd_found(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_found_command(
            message,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._get_user_recent_findings,
        )


def register_cmd_list(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("list"))
    @_router_globals.requires("user")
    async def cmd_list(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_list_command(
            message,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._safe_reply,
        )


def register_cmd_show(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("show"))
    @_router_globals.requires("owner", touches_product_id=True)
    async def cmd_show(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_show_command(
            message, _resolve_user(current_user), _resolve_store(), _router_globals._safe_reply
        )


def register_cmd_pause(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]
    channel = outer["channel"]

    @router.message(_router_globals.Command("pause"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cmd_pause(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_pause_command(
            message,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            getattr(channel, "bot", None),
        )


def register_cmd_pause_all(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]
    channel = outer["channel"]

    @router.message(_router_globals.Command("pause_all"))
    @_router_globals.requires("user")
    async def cmd_pause_all(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        user = _resolve_user(current_user)
        if user and user.role == "owner":
            await _router_globals.handle_pause_all_command(
                message, _resolve_db(), getattr(channel, "bot", None)
            )
        else:
            await _router_globals.handle_pause_command(
                message, user, _resolve_store(), _resolve_db(), getattr(channel, "bot", None)
            )


def register_cmd_resume(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]
    channel = outer["channel"]

    @router.message(_router_globals.Command("resume"))
    @_router_globals.requires("user")
    async def cmd_resume(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_resume_command(
            message,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            getattr(channel, "bot", None),
        )


def register_cmd_resume_all(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]
    channel = outer["channel"]

    @router.message(_router_globals.Command("resume_all"))
    @_router_globals.requires("user")
    async def cmd_resume_all(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        user = _resolve_user(current_user)
        if user and user.role == "owner":
            await _router_globals.handle_resume_all_command(
                message, _resolve_db(), message.bot or getattr(channel, "bot", None)
            )
        else:
            await _router_globals.handle_resume_command(
                message, user, _resolve_store(), _resolve_db(), getattr(channel, "bot", None)
            )


def register_cb_pause_all_confirm(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]
    _resolve_db = outer["_resolve_db"]

    @router.callback_query(_router_globals.F.data == "pause_all_confirm")
    @_router_globals.requires("user")
    async def cb_pause_all_confirm(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_pause_all_confirm_callback(
            callback, _resolve_user(current_user), _resolve_store(), _router_globals.get_settings(), _resolve_db()
        )


def register_cb_pause_all_cancel(router: Router, outer: dict[str, Any]) -> None:
    @router.callback_query(_router_globals.F.data == "pause_all_cancel")
    @_router_globals.requires("user")
    async def cb_pause_all_cancel(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_pause_all_cancel_callback(callback)


def register_cb_resume_all_confirm(router: Router, outer: dict[str, Any]) -> None:
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]
    _resolve_db = outer["_resolve_db"]

    @router.callback_query(_router_globals.F.data == "resume_all_confirm")
    @_router_globals.requires("user")
    async def cb_resume_all_confirm(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_resume_all_confirm_callback(
            callback, _resolve_user(current_user), _resolve_store(), _router_globals.get_settings(), _resolve_db()
        )


def register_cb_resume_all_cancel(router: Router, outer: dict[str, Any]) -> None:
    @router.callback_query(_router_globals.F.data == "resume_all_cancel")
    @_router_globals.requires("user")
    async def cb_resume_all_cancel(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_resume_all_cancel_callback(callback)


def register_cmd_remove(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("remove"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cmd_remove(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_remove_command(
            message,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals._safe_reply,
        )


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cmd_found(router, outer)
    register_cmd_list(router, outer)
    register_cmd_show(router, outer)
    register_cmd_pause(router, outer)
    register_cmd_pause_all(router, outer)
    register_cmd_resume(router, outer)
    register_cmd_resume_all(router, outer)
    register_cb_pause_all_confirm(router, outer)
    register_cb_pause_all_cancel(router, outer)
    register_cb_resume_all_confirm(router, outer)
    register_cb_resume_all_cancel(router, outer)
    register_cmd_remove(router, outer)
