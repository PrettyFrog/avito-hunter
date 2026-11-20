from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command as Command, CommandObject as CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated as ChatMemberUpdated,
    InlineKeyboardMarkup,
    Message as Message,
)

from hunter.core.config import get_settings
from hunter.core.db import Database, get_db as get_db
from hunter.core.models import User, now_utc as now_utc
from hunter.core.product import ProductStore, get_product_store as get_product_store
from hunter.notify.telegram_router_parts.originalproductstore_01 import (
    _ADMIN_CONFIRMATIONS as _ADMIN_CONFIRMATIONS,
    _BROADCAST_PAYLOADS as _BROADCAST_PAYLOADS,
    _COMMAND_THROTTLE_SEC as _COMMAND_THROTTLE_SEC,
    _LABELS as _LABELS,
    _MAX_PHOTO_SIZE_BYTES as _MAX_PHOTO_SIZE_BYTES,
    _OriginalProductStore,
    _chat_locks as _chat_locks,
    _get_chat_lock as _get_chat_lock,
    _safe_edit_text as _safe_edit_text,
    _safe_reply as _safe_reply,
    attach_auth_middleware,
    calculate_profiles_forecast_worst_case as calculate_profiles_forecast_worst_case,
    check_user_blocked_status as check_user_blocked_status,
    main_menu_keyboard as main_menu_keyboard,
    parse_start_args as parse_start_args,
    resolve_db_helper,
    resolve_store_helper,
    resolve_user_helper,
    validate_start_invite as validate_start_invite,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_start_09 import (
    bind_public_telegram_destination as bind_public_telegram_destination,
    create_access_request_and_notify_owner as create_access_request_and_notify_owner,
    handle_start_command as handle_start_command,
    handle_known_active_user_start as handle_known_active_user_start,
    handle_stranger_start as handle_stranger_start,
    register_invite_user_and_notify as register_invite_user_and_notify,
    validate_and_gate_access_request as validate_and_gate_access_request,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_owner_acc_ok_13 import (
    handle_onboarding_done as handle_onboarding_done,
    handle_onboarding_step_2 as handle_onboarding_step_2,
    handle_onboarding_step_3 as handle_onboarding_step_3,
    process_owner_access_approval as process_owner_access_approval,
    process_owner_access_denial as process_owner_access_denial,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_invite_23 import (
    handle_create_invite_command as handle_create_invite_command,
    handle_faq_command as handle_faq_command,
    handle_help_command as handle_help_command,
    handle_list_invites_command as handle_list_invites_command,
    handle_revoke_invite_callback as handle_revoke_invite_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_faq_menu_33 import (
    handle_broadcast_cancel_callback as handle_broadcast_cancel_callback,
    handle_broadcast_command as handle_broadcast_command,
    handle_broadcast_send_callback as handle_broadcast_send_callback,
    handle_faq_item_callback as handle_faq_item_callback,
    handle_faq_menu_callback as handle_faq_menu_callback,
    handle_guide_command as handle_guide_command,
    handle_quiet_command as handle_quiet_command,
    handle_support_command as handle_support_command,
    handle_ticket_reply_callback as handle_ticket_reply_callback,
    handle_tickets_command as handle_tickets_command,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_delete_me_53 import (
    admin_root_keyboard,
    handle_admin_command as handle_admin_command,
    handle_admin_root_callback as handle_admin_root_callback,
    handle_delete_me_cancel_callback as handle_delete_me_cancel_callback,
    handle_delete_me_command as handle_delete_me_command,
    handle_delete_me_confirm_callback as handle_delete_me_confirm_callback,
    render_admin_user_view,
    render_admin_users_list,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_admin_u_pause_69 import (
    handle_admin_u_block_callback as handle_admin_u_block_callback,
    handle_admin_u_block_confirm_callback as handle_admin_u_block_confirm_callback,
    handle_admin_u_del_callback as handle_admin_u_del_callback,
    handle_admin_u_pause_callback as handle_admin_u_pause_callback,
    handle_admin_u_resume_callback as handle_admin_u_resume_callback,
    handle_admin_u_tier_callback as handle_admin_u_tier_callback,
    handle_admin_u_tier_set_callback as handle_admin_u_tier_set_callback,
    handle_admin_u_unblock_callback as handle_admin_u_unblock_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_admin_u_del_confirm_85 import (
    handle_admin_broadcast_callback as handle_admin_broadcast_callback,
    handle_admin_inv_create_callback as handle_admin_inv_create_callback,
    handle_admin_inv_revoke_callback as handle_admin_inv_revoke_callback,
    handle_admin_invites_callback as handle_admin_invites_callback,
    handle_admin_stats_callback as handle_admin_stats_callback,
    handle_admin_system_callback as handle_admin_system_callback,
    handle_admin_u_del_confirm_callback as handle_admin_u_del_confirm_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router__format_errors_report_98 import (
    format_errors_report_helper,
    handle_admin_errors_callback as handle_admin_errors_callback,
    handle_errors_command as handle_errors_command,
    handle_my_command as handle_my_command,
    handle_settings_command as handle_settings_command,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_my_pause_109 import (
    handle_my_del_ask_callback as handle_my_del_ask_callback,
    handle_my_del_cancel_callback as handle_my_del_cancel_callback,
    handle_my_del_do_callback as handle_my_del_do_callback,
    handle_my_findings_callback as handle_my_findings_callback,
    handle_my_pause_callback as handle_my_pause_callback,
    handle_my_photo_callback as handle_my_photo_callback,
    handle_my_price_callback as handle_my_price_callback,
    handle_my_resume_callback as handle_my_resume_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_found_125 import (
    handle_found_command as handle_found_command,
    handle_list_command as handle_list_command,
    handle_pause_all_command as handle_pause_all_command,
    handle_pause_command as handle_pause_command,
    handle_resume_command as handle_resume_command,
    handle_show_command as handle_show_command,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_registration_136 import (
    handle_pause_all_cancel_callback as handle_pause_all_cancel_callback,
    handle_pause_all_confirm_callback as handle_pause_all_confirm_callback,
    handle_remove_cancel_callback as handle_remove_cancel_callback,
    handle_remove_command as handle_remove_command,
    handle_remove_confirm_callback as handle_remove_confirm_callback,
    handle_resume_all_cancel_callback as handle_resume_all_cancel_callback,
    handle_resume_all_command as handle_resume_all_command,
    handle_resume_all_confirm_callback as handle_resume_all_confirm_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_edit_154 import (
    handle_cap_command as handle_cap_command,
    handle_edit_command as handle_edit_command,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_handle_apply_stage_158 import (
    handle_apply_stage_callback as handle_apply_stage_callback,
    handle_budget_command as handle_budget_command,
    handle_limits_command as handle_limits_command,
    handle_users_command as handle_users_command,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_stats_166 import (
    handle_ref_command as handle_ref_command,
    handle_stats_command as handle_stats_command,
    handle_status_command as handle_status_command,
    handle_why_command as handle_why_command,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cmd_photocheck_174 import (
    handle_add_command as handle_add_command,
    handle_photocheck_command as handle_photocheck_command,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_start_add_178 import (
    handle_add_edit_price_callback as handle_add_edit_price_callback,
    handle_add_faq_url_callback as handle_add_faq_url_callback,
    handle_add_photo_proceed_callback as handle_add_photo_proceed_callback,
    handle_add_policy_callback as handle_add_policy_callback,
    handle_add_reqs_skip_callback as handle_add_reqs_skip_callback,
    handle_add_verify_edit_url_callback as handle_add_verify_edit_url_callback,
    handle_add_verify_retry_callback as handle_add_verify_retry_callback,
    handle_start_add_callback as handle_start_add_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_cb_add_confirm_launch_194 import (
    handle_add_confirm_launch_callback as handle_add_confirm_launch_callback,
    handle_dialog_early_steps as handle_dialog_early_steps,
    handle_stale_del_callback as handle_stale_del_callback,
    handle_stale_keep_callback as handle_stale_keep_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_on_dialog_text_201 import (
    handle_dialog_text_message as handle_dialog_text_message,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_on_photo_attachment_204 import (
    handle_photo_attachment_message as handle_photo_attachment_message,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_on_alert_ref_add_208 import (
    handle_alert_pause_prod_callback as handle_alert_pause_prod_callback,
    handle_alert_ref_add_callback as handle_alert_ref_add_callback,
    handle_feedback_callback as handle_feedback_callback,
    handle_ref_build_callback,
    handle_ref_cancel_callback as handle_ref_cancel_callback,
    handle_ref_confirm_callback as handle_ref_confirm_callback,
    handle_ref_redo_callback,
)
from hunter.notify.telegram_router_parts.setup_telegram_router_on_audit_feedback_222 import (
    handle_add_cancel_callback as handle_add_cancel_callback,
    handle_add_confirm_legacy_callback as handle_add_confirm_legacy_callback,
    handle_add_edit_callback as handle_add_edit_callback,
    handle_audit_feedback_callback as handle_audit_feedback_callback,
    handle_my_chat_member_event as handle_my_chat_member_event,
    handle_toggle_archive_callback as handle_toggle_archive_callback,
)
from hunter.notify.telegram_router_parts.format_product_card_234 import (
    _format_product_card as _format_product_card,
    _get_findings_for_product as _get_findings_for_product,
    _get_last_found_time_ru as _get_last_found_time_ru,
    _get_user_recent_findings as _get_user_recent_findings,
    _product_card_keyboard as _product_card_keyboard,
)
from hunter.notify.telegram_router_parts.start_add_verification_user_237 import (
    check_test_fetch_cache as check_test_fetch_cache,
    execute_test_fetch_job as execute_test_fetch_job,
    generate_user_product_id as generate_user_product_id,
    validate_and_save_add_url as validate_and_save_add_url,
)
from hunter.notify.telegram_router_parts.start_add_verification_user_239 import (
    build_temp_product_profile as build_temp_product_profile,
    fetch_test_hits_admin as fetch_test_hits_admin,
    finalize_add_verification_user as finalize_add_verification_user,
    generate_admin_product_id as generate_admin_product_id,
    render_add_confirm_card as render_add_confirm_card,
    validate_admin_add_url as validate_admin_add_url,
)
from hunter.notify.telegram_router_parts.start_add_verification_241 import (
    build_admin_temp_profile as build_admin_temp_profile,
    check_admin_add_budget as check_admin_add_budget,
    evaluate_hits_match as evaluate_hits_match,
    finalize_admin_add_verification as finalize_admin_add_verification,
)
from hunter.notify.telegram_router_parts.render_summary_card_243 import (
    build_summary_card_keyboard as build_summary_card_keyboard,
    _format_summary_card_text as _format_summary_card_text,
    send_summary_card as send_summary_card,
)
from hunter.notify.messages_ru import (
    ADD_PREVIEW_CHECKING as ADD_PREVIEW_CHECKING,
    INVITE_ALREADY_USED as INVITE_ALREADY_USED,
    OWNER_START_GREETING as OWNER_START_GREETING,
)
from hunter.notify.security import requires
from hunter.notify.telegram_handlers.channel_binding import register_vk_binding_command
from hunter.notify.telegram_handlers.public_search_routes import register_search_menu
from hunter.notify.telegram_handlers.public_add import register_public_add_callbacks


from hunter.notify.telegram_router_parts.register_routes_01 import register_pack as register_routes_01
from hunter.notify.telegram_router_parts.register_routes_02 import register_pack as register_routes_02
from hunter.notify.telegram_router_parts.register_routes_03 import register_pack as register_routes_03
from hunter.notify.telegram_router_parts.register_routes_04 import register_pack as register_routes_04
from hunter.notify.telegram_router_parts.register_routes_05 import register_pack as register_routes_05
from hunter.notify.telegram_router_parts.register_routes_06 import register_pack as register_routes_06
from hunter.notify.telegram_router_parts.register_routes_07 import register_pack as register_routes_07
from hunter.notify.telegram_router_parts.register_routes_08 import register_pack as register_routes_08
from hunter.notify.telegram_router_parts.register_routes_09 import register_pack as register_routes_09
from hunter.notify.telegram_router_parts.register_routes_10 import register_pack as register_routes_10
from hunter.notify.telegram_router_parts.register_routes_11 import register_pack as register_routes_11
from hunter.notify.telegram_router_parts.register_routes_12 import register_pack as register_routes_12
from hunter.notify.telegram_router_parts.router_add_verification import (
    _render_summary_card as _render_summary_card,
    _start_add_verification as _start_add_verification,
    _start_add_verification_user as _start_add_verification_user,
)

def setup_telegram_router(
    channel: Any, db: Database | None = None, subscription_service: Any = None
) -> Router:
    router = Router()
    owner_chat_id = str(channel.chat_id)
    attach_auth_middleware(router, owner_chat_id, db=db)
    def _resolve_db(injected_db: Database | None = None) -> Database:
        return resolve_db_helper(injected_db, db)
    def _resolve_user(user: User | None = None) -> User:
        return resolve_user_helper(user, owner_chat_id)
    def _resolve_store() -> ProductStore:
        if ProductStore is not _OriginalProductStore:
            return ProductStore()
        return resolve_store_helper()
    register_search_menu(router, _resolve_db)
    register_public_add_callbacks(router, _resolve_db, subscription_service)
    register_vk_binding_command(router, getattr(subscription_service, "channel_binding_service", None))
    register_routes_01(router, locals())
    register_routes_02(router, locals())
    register_routes_03(router, locals())
    _admin_root_keyboard = admin_root_keyboard
    register_routes_04(router, locals())
    async def _render_admin_users_list(msg: Any, db_inst: Database) -> None:
        await render_admin_users_list(msg, db_inst, _resolve_store())
    async def _render_admin_user_view(msg: Any, uid: int, current_user: User, db_inst: Database) -> None:
        await render_admin_user_view(msg, uid, current_user, db_inst, _resolve_store())
    register_routes_05(router, locals())
    register_routes_06(router, locals())
    def _format_errors_report(data: dict[str, Any], page: int = 0, page_size: int = 15) -> tuple[str, InlineKeyboardMarkup]:
        return format_errors_report_helper(data, page=page, page_size=page_size)
    register_routes_07(router, locals())
    register_routes_08(router, locals())
    register_routes_09(router, locals())
    register_routes_10(router, locals())
    register_routes_11(router, locals())
    @router.callback_query(F.data.startswith("ref_build:"))
    @requires("owner")
    async def on_ref_build(query: CallbackQuery, current_user: User | None = None) -> None:
        await handle_ref_build_callback(
            query, _resolve_user(current_user), store=ProductStore(), settings=get_settings()
        )

    @router.callback_query(F.data.startswith("ref_redo:"))
    @requires("owner")
    async def on_ref_redo(query: CallbackQuery, current_user: User | None = None) -> None:
        await handle_ref_redo_callback(
            query, _resolve_user(current_user), on_ref_build, store=ProductStore()
        )

    @router.callback_query(F.data == "noop")
    @requires("user")
    async def on_noop_badge(query: CallbackQuery, current_user: User | None = None) -> None:
        await query.answer()

    register_routes_12(router, locals())
    return router
