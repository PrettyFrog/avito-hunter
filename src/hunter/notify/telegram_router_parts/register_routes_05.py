"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations
from typing import Any
from aiogram import Router
from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cb_admin_users(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_users_list = outer["_render_admin_users_list"]
    _resolve_db = outer["_resolve_db"]

    @router.callback_query(_router_globals.F.data == "admin:users")
    @_router_globals.requires("owner")
    async def cb_admin_users(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        db_inst = _resolve_db(db)
        await _render_admin_users_list(query.message, db_inst)
        await query.answer()


def register_cb_admin_u_view(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_user_view = outer["_render_admin_user_view"]
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_view:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_view(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        current_user = _resolve_user(current_user)
        uid = int((query.data or "").split(":", 2)[2])
        db_inst = _resolve_db(db)
        await _render_admin_user_view(query.message, uid, current_user, db_inst)
        await query.answer()


def register_cb_admin_u_pause(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_user_view = outer["_render_admin_user_view"]
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_pause:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_pause(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_pause_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(db),
            _resolve_store(),
            _render_admin_user_view,
        )


def register_cb_admin_u_resume(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_user_view = outer["_render_admin_user_view"]
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_resume:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_resume(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_resume_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(db),
            _resolve_store(),
            _render_admin_user_view,
        )


def register_cb_admin_u_block(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_block:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_block(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_block_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(db),
            _router_globals._ADMIN_CONFIRMATIONS,
        )


def register_cb_admin_u_block_confirm(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_user_view = outer["_render_admin_user_view"]
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_block_confirm:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_block_confirm(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_block_confirm_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(db),
            _resolve_store(),
            _router_globals._ADMIN_CONFIRMATIONS,
            _render_admin_user_view,
        )


def register_cb_admin_u_unblock(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_user_view = outer["_render_admin_user_view"]
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_unblock:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_unblock(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_unblock_callback(
            query, _resolve_user(current_user), _resolve_db(db), _render_admin_user_view
        )


def register_cb_admin_u_tier(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_tier:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_tier(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_tier_callback(query, _resolve_user(current_user))


def register_cb_admin_u_tier_set(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_user_view = outer["_render_admin_user_view"]
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_tier_set:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_tier_set(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_tier_set_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(db),
            _resolve_store(),
            _render_admin_user_view,
        )


def register_cb_admin_u_del(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_del:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_del(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_del_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(db),
            _router_globals._ADMIN_CONFIRMATIONS,
        )


def register_cb_admin_u_del_confirm(router: Router, outer: dict[str, Any]) -> None:
    _render_admin_users_list = outer["_render_admin_users_list"]
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("admin:u_del_confirm:"))
    @_router_globals.requires("owner")
    async def cb_admin_u_del_confirm(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_u_del_confirm_callback(
            query,
            _resolve_user(current_user),
            _resolve_db(db),
            _resolve_store(),
            _router_globals._ADMIN_CONFIRMATIONS,
            _render_admin_users_list,
        )


def register_cb_admin_invites(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "admin:invites")
    @_router_globals.requires("owner")
    async def cb_admin_invites(
        query: _router_globals.CallbackQuery,
        current_user: _router_globals.User | None = None,
        db: _router_globals.Database | None = None,
    ) -> None:
        await _router_globals.handle_admin_invites_callback(
            query, _resolve_user(current_user), _resolve_db(db)
        )


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cb_admin_users(router, outer)
    register_cb_admin_u_view(router, outer)
    register_cb_admin_u_pause(router, outer)
    register_cb_admin_u_resume(router, outer)
    register_cb_admin_u_block(router, outer)
    register_cb_admin_u_block_confirm(router, outer)
    register_cb_admin_u_unblock(router, outer)
    register_cb_admin_u_tier(router, outer)
    register_cb_admin_u_tier_set(router, outer)
    register_cb_admin_u_del(router, outer)
    register_cb_admin_u_del_confirm(router, outer)
    register_cb_admin_invites(router, outer)
