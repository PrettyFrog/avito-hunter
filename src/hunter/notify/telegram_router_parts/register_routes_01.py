"""Telegram route registration slice; generated from the stable router facade."""

from __future__ import annotations

from typing import Any

from aiogram import Router

from hunter.notify.telegram_router_parts import router_globals as _router_globals


def register_cmd_start(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_store = outer["_resolve_store"]
    owner_chat_id = outer["owner_chat_id"]
    subscription_service = outer["subscription_service"]

    @router.message(_router_globals.Command("start"))
    @_router_globals.requires("public")
    async def cmd_start(
        message: _router_globals.Message,
        command: _router_globals.CommandObject | None = None,
        current_user: _router_globals.User | None = None,
    ) -> None:
        await _router_globals.handle_start_command(
            message,
            command,
            current_user,
            _resolve_db,
            _resolve_store,
            owner_chat_id,
            subscription_service,
        )


def register_cb_req_access(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]

    @router.callback_query(_router_globals.F.data == "req_access")
    @_router_globals.requires("public")
    async def cb_req_access(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        db_inst = _resolve_db()
        user_id = callback.from_user.id
        user_display = (
            callback.from_user.full_name or callback.from_user.first_name
            if callback.from_user
            else "Пользователь"
        )
        username = callback.from_user.username if callback.from_user else None

        allowed = await _router_globals.validate_and_gate_access_request(
            db_inst=db_inst,
            callback=callback,
            user_id=user_id,
            current_user=current_user,
        )
        if not allowed:
            return

        await _router_globals.create_access_request_and_notify_owner(
            db_inst=db_inst,
            callback=callback,
            user_id=user_id,
            username=username,
            user_display=user_display,
        )


def register_cb_owner_acc_ok(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("acc_ok:"))
    @_router_globals.requires("owner")
    async def cb_owner_acc_ok(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db()
        parts = (callback.data or "").split(":")
        if len(parts) < 3:
            return
        tier = parts[1]  # "friend" or "family"
        req_id = int(parts[2])
        await _router_globals.process_owner_access_approval(
            callback=callback,
            db_inst=db_inst,
            current_user=current_user,
            tier=tier,
            req_id=req_id,
        )


def register_cb_owner_acc_deny(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("acc_deny:"))
    @_router_globals.requires("owner")
    async def cb_owner_acc_deny(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db()
        parts = (callback.data or "").split(":")
        if len(parts) < 2:
            return
        req_id = int(parts[1])
        await _router_globals.process_owner_access_denial(
            callback=callback,
            db_inst=db_inst,
            current_user=current_user,
            req_id=req_id,
        )


def register_cb_onb_2(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "onb:2")
    @_router_globals.requires("user")
    async def cb_onb_2(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        _resolve_user(current_user)
        await _router_globals.handle_onboarding_step_2(callback)


def register_cb_onb_3(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "onb:3")
    @_router_globals.requires("user")
    async def cb_onb_3(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        _resolve_user(current_user)
        await _router_globals.handle_onboarding_step_3(callback)


def register_cb_onb_done(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data == "onb:done")
    @_router_globals.requires("user")
    async def cb_onb_done(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        current_user = _resolve_user(current_user)
        await _router_globals.handle_onboarding_done(callback, current_user)


def register_cmd_invite(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("invite"))
    @_router_globals.requires("owner")
    async def cmd_invite(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db()
        await _router_globals.handle_create_invite_command(message, db_inst, current_user)


def register_cmd_invites(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("invites"))
    @_router_globals.requires("owner")
    async def cmd_invites(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        current_user = _resolve_user(current_user)
        db_inst = _resolve_db()
        await _router_globals.handle_list_invites_command(message, db_inst, current_user)


def register_cb_revoke_inv(router: Router, outer: dict[str, Any]) -> None:
    _resolve_db = outer["_resolve_db"]
    _resolve_user = outer["_resolve_user"]

    @router.callback_query(_router_globals.F.data.startswith("revoke_inv:"))
    @_router_globals.requires("owner")
    async def cb_revoke_inv(
        callback: _router_globals.CallbackQuery, current_user: _router_globals.User | None = None
    ) -> None:
        _resolve_user(current_user)
        db_inst = _resolve_db()
        await _router_globals.handle_revoke_invite_callback(callback, db_inst)


def register_cmd_help(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("help"))
    @_router_globals.requires("user")
    async def cmd_help(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        current_user = _resolve_user(current_user)
        await _router_globals.handle_help_command(message, current_user)


def register_cmd_faq(router: Router, outer: dict[str, Any]) -> None:
    _resolve_user = outer["_resolve_user"]

    @router.message(_router_globals.Command("faq"))
    @router.message(_router_globals.F.text == "❓ Помощь")
    @_router_globals.requires("user")
    async def cmd_faq(
        message: _router_globals.Message, current_user: _router_globals.User | None = None
    ) -> None:
        _resolve_user(current_user)
        await _router_globals.handle_faq_command(message)


def register_pack(router: Router, outer: dict[str, Any]) -> None:
    register_cmd_start(router, outer)
    register_cb_req_access(router, outer)
    register_cb_owner_acc_ok(router, outer)
    register_cb_owner_acc_deny(router, outer)
    register_cb_onb_2(router, outer)
    register_cb_onb_3(router, outer)
    register_cb_onb_done(router, outer)
    register_cmd_invite(router, outer)
    register_cmd_invites(router, outer)
    register_cb_revoke_inv(router, outer)
    register_cmd_help(router, outer)
    register_cmd_faq(router, outer)
