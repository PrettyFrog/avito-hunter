from __future__ import annotations
from typing import Any
from hunter.core.db_parts.database_block_user_by_owner_19 import answer_support_ticket_db, block_user_by_owner_db, change_user_tier_db, create_support_ticket_db, get_broadcast_history_db, get_open_support_tickets_db, get_support_ticket_db, get_user_support_tickets_count_24h_db, log_broadcast_db, mark_user_bot_blocked_db, unblock_user_by_owner_db
from hunter.core.db_parts.database_checkpoint_13 import cleanup_old_records_db, create_user_db, get_runtime_db, get_triage_verdicts_db, set_runtime_db, set_triage_verdicts_db
from hunter.core.db_parts.database_get_user_by_telegram_id_16 import delete_user_data_db, find_user_by_identifier_db, get_active_users_db, get_all_users_db, get_owner_user_db, get_user_by_id_db, get_user_by_telegram_id_db, log_user_event_db, set_user_status_db, update_user_last_seen_db


class _DatabaseApi01:
        async def cleanup_old_records(
            self,
            job_retention_days: int = 30,
            request_log_retention_days: int = 14,
            memory_log_retention_days: int = 14,
            events_retention_days: int = 30,
            deliveries_retention_days: int = 14,
            images_retention_days: int = 30,
            images_dir: Any = None,
        ) -> dict[str, int]:
            """Prunes historical completed jobs and ephemeral debug logs while preserving
            listings, evaluations, notifications, and user configurations.
            """
            return await cleanup_old_records_db(
                self,
                job_retention_days=job_retention_days,
                request_log_retention_days=request_log_retention_days,
                memory_log_retention_days=memory_log_retention_days,
                events_retention_days=events_retention_days,
                deliveries_retention_days=deliveries_retention_days,
                images_retention_days=images_retention_days,
                images_dir=images_dir,
            )

        async def get_runtime(self, key: str, default: str | None = None) -> str | None:
            return await get_runtime_db(self, key, default=default)

        async def set_runtime(self, key: str, value: str) -> None:
            await set_runtime_db(self, key, value)

        async def get_triage_verdicts(
            self, title_hashes: list[str], product_id: str | None = None
        ) -> dict[str, str]:
            """Return {title_hash: verdict} for the hashes already present in the cache."""
            return await get_triage_verdicts_db(self, title_hashes, product_id=product_id)

        async def set_triage_verdicts(
            self, rows: list[tuple[str, str, str | None, str | None]]
        ) -> None:
            """Upsert (title_hash, verdict, product_id, title) rows into the triage cache."""
            await set_triage_verdicts_db(self, rows)

        async def create_user(
            self,
            telegram_user_id: int,
            chat_id: int | None = None,
            username: str | None = None,
            display_name: str | None = None,
            role: str = "user",
            status: str = "active",
            tier: str = "friend",
            max_products: int = 3,
            invited_by: int | None = None,
            min_interval_minutes: int | None = None,
            max_alerts_per_hour: int | None = None,
        ) -> int:
            return await create_user_db(
                self,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                username=username,
                display_name=display_name,
                role=role,
                status=status,
                tier=tier,
                max_products=max_products,
                invited_by=invited_by,
                min_interval_minutes=min_interval_minutes,
                max_alerts_per_hour=max_alerts_per_hour,
            )

        async def get_user_by_telegram_id(self, telegram_user_id: int) -> dict | None:
            return await get_user_by_telegram_id_db(self, telegram_user_id)

        async def get_user_by_id(self, user_id: int) -> dict | None:
            return await get_user_by_id_db(self, user_id)

        async def get_owner_user(self) -> dict | None:
            return await get_owner_user_db(self)

        async def set_user_status(self, user_id: int, status: str, last_error: str | None = None) -> None:
            await set_user_status_db(self, user_id, status, last_error=last_error)

        async def update_user_last_seen(self, user_id: int, username: str | None = None, display_name: str | None = None) -> None:
            await update_user_last_seen_db(self, user_id, username=username, display_name=display_name)

        async def log_user_event(self, user_id: int, kind: str, details: Any = None) -> None:
            await log_user_event_db(self, user_id, kind, details=details)

        async def get_active_users(self) -> list[dict]:
            return await get_active_users_db(self)

        async def get_all_users(self) -> list[dict]:
            return await get_all_users_db(self)

        async def find_user_by_identifier(self, ident: str | int) -> dict | None:
            """Find user by internal id, telegram_user_id, or username (with or without @)."""
            return await find_user_by_identifier_db(self, ident)

        async def delete_user_data(self, user_id: int) -> dict[str, int]:
            """Purges all user data across all tables and nullifies identifiers in users table.
            Leaves anonymized counters. Owner cannot be deleted.
            """
            return await delete_user_data_db(self, user_id)

        async def block_user_by_owner(self, user_id: int) -> None:
            """Blocks user explicitly by owner. Owner cannot be blocked."""
            await block_user_by_owner_db(self, user_id)

        async def unblock_user_by_owner(self, user_id: int) -> None:
            """Unblocks user by owner."""
            await unblock_user_by_owner_db(self, user_id)

        async def mark_user_bot_blocked(self, user_id: int) -> None:
            """Marks user as bot_blocked when bot is blocked in telegram.
            Owner is never marked. Does NOT overwrite blocked_by_owner.
            """
            await mark_user_bot_blocked_db(self, user_id)

        async def change_user_tier(
            self,
            user_id: int,
            new_tier: str,
            min_interval_minutes: int | None = None,
            max_products: int | None = None,
            max_alerts_per_hour: int | None = None,
        ) -> None:
            """Changes user tier and updates default limits."""
            await change_user_tier_db(
                self,
                user_id,
                new_tier,
                min_interval_minutes=min_interval_minutes,
                max_products=max_products,
                max_alerts_per_hour=max_alerts_per_hour,
            )

        async def create_support_ticket(self, user_id: int, chat_id: int, text: str) -> int:
            return await create_support_ticket_db(self, user_id, chat_id, text)

        async def get_open_support_tickets(self) -> list[dict]:
            return await get_open_support_tickets_db(self)

        async def get_support_ticket(self, ticket_id: int) -> dict | None:
            return await get_support_ticket_db(self, ticket_id)

        async def answer_support_ticket(
            self,
            ticket_id: int,
            answered_by: int,
            response_text: str,
            status: str = "answered",
            error: str | None = None,
        ) -> None:
            await answer_support_ticket_db(
                self,
                ticket_id,
                answered_by,
                response_text,
                status=status,
                error=error,
            )

        async def get_user_support_tickets_count_24h(self, user_id: int) -> int:
            return await get_user_support_tickets_count_24h_db(self, user_id)

        async def log_broadcast(
            self,
            sender_user_id: int | None,
            target_audience: str,
            text: str,
            delivered_count: int,
            failed_count: int,
        ) -> int:
            return await log_broadcast_db(
                self,
                sender_user_id,
                target_audience,
                text,
                delivered_count,
                failed_count,
            )

        async def get_broadcast_history(self, limit: int = 10) -> list[dict]:
            return await get_broadcast_history_db(self, limit=limit)
