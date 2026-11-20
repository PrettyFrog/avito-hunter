"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cb_admin_inv_create(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]
    channel = outer["channel"]

    @router.callback_query(_router_globals.F.data.startswith("admin:inv_create:"))
    @_router_globals.requires("owner")
    async def cb_admin_inv_create(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_inv_create_callback(
            query, _resolve_user(current_user), _resolve_db(db), getattr(channel, "bot", None)
        )


def register_cb_admin_inv_revoke(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:inv_revoke:"))
    @_router_globals.requires("owner")
    async def cb_admin_inv_revoke(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_inv_revoke_callback(
            query, _resolve_user(current_user), _resolve_db(db)
        )


def register_cb_admin_broadcast(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "admin:broadcast")
    @_router_globals.requires("owner")
    async def cb_admin_broadcast(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_broadcast_callback(
            query, _resolve_user(current_user), _resolve_db(db)
        )


def register_cb_admin_stats(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]

    @router.callback_query(_router_globals.F.data == "admin:stats")
    @_router_globals.requires("owner")
    async def cb_admin_stats(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_stats_callback(query, _resolve_db(db), _resolve_store())


def register_cb_admin_system(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]

    @router.callback_query(_router_globals.F.data == "admin:system")
    @_router_globals.requires("owner")
    async def cb_admin_system(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_system_callback(query, _resolve_db(db))


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cb_admin_inv_create(router, outer)
    register_cb_admin_inv_revoke(router, outer)
    register_cb_admin_broadcast(router, outer)
    register_cb_admin_stats(router, outer)
    register_cb_admin_system(router, outer)
