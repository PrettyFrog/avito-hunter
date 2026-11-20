"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cmd_admin(router: Router, outer: dict[str, Any]) -> None:
    @router.message(_router_globals.Command("admin"))
    @_router_globals.requires("owner")
    async def cmd_admin(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_admin_command(message, _router_globals._safe_reply)


def register_cb_admin_root(router: Router, outer: dict[str, Any]) -> None:
    @router.callback_query(_router_globals.F.data == "admin:root")
    @_router_globals.requires("owner")
    async def cb_admin_root(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_admin_root_callback(query)


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cmd_admin(router, outer)
    register_cb_admin_root(router, outer)
