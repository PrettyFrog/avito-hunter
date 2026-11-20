"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations
from typing import Any
from aiogram import Router
from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_on_remove_confirm(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("rm_ok:"))
    @_router_globals.requires("owner", touches_product_id=True)
    async def on_remove_confirm(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_remove_confirm_callback(
            query, _resolve_user(current_user), _router_globals.get_settings()
        )


def register_on_remove_cancel(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("rm_no:"))
    @_router_globals.requires("owner", touches_product_id=True)
    async def on_remove_cancel(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_remove_cancel_callback(
            query, _resolve_user(current_user), _router_globals.get_settings()
        )


def register_cmd_edit(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("edit"))
    @_router_globals.requires("user", touches_product_id=True)
    async def cmd_edit(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_edit_command(
            message,
            _resolve_user(current_user),
            _resolve_store(),
            _resolve_db(),
            _router_globals.get_settings(),
            _router_globals.calculate_profiles_forecast_worst_case,
        )


def register_cmd_limits(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]

    @router.message(_router_globals.Command("limits"))
    @_router_globals.requires("owner")
    async def cmd_limits(
        message: _router_globals.Message,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_limits_command(message, _resolve_db(db))


def register_cmd_users(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]

    @router.message(_router_globals.Command("users"))
    @_router_globals.requires("owner")
    async def cmd_users(
        message: _router_globals.Message,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_users_command(
            message, _resolve_db(db), _resolve_store(), _router_globals._safe_reply
        )


def register_cmd_stats(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("stats"))
    @_router_globals.requires("owner")
    async def cmd_stats(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_stats_command(
            message,
            _resolve_user(current_user),
            _resolve_db(),
            _resolve_store(),
            _router_globals._safe_reply,
        )


def register_cmd_status(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("status"))
    @_router_globals.requires("user")
    async def cmd_status(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_status_command(
            message,
            _resolve_user(current_user),
            _resolve_db(),
            _resolve_store(),
            _router_globals._safe_reply,
        )


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_on_remove_confirm(router, outer)
    register_on_remove_cancel(router, outer)
    register_cmd_edit(router, outer)
    register_cmd_limits(router, outer)
    register_cmd_users(router, outer)
    register_cmd_stats(router, outer)
    register_cmd_status(router, outer)
