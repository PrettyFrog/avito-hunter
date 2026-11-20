"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cb_delme_cancel(router: Router, outer: dict[str, Any]) -> None:
    @router.callback_query(_router_globals.F.data == "delme:cancel")
    @_router_globals.requires("user")
    async def cb_delme_cancel(
        query: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        await _router_globals.handle_delete_me_cancel_callback(query)


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cb_delme_cancel(router, outer)
