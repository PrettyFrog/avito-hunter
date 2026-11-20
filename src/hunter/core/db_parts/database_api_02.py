from __future__ import annotations
from typing import Any
from hunter.core.db_parts.database_create_invite_21 import consume_invite_atomic_db, create_access_request_db, create_invite_db, get_access_request_db, get_access_requests_last_hour_count_db, get_active_invites_count_db, get_active_invites_db, get_invite_by_hash_db, get_invite_by_id_db, get_pending_access_requests_count_db, get_recent_access_request_time_db, get_rejected_access_requests_count_db, revoke_invite_db
from hunter.core.db_parts.database_pop_digest_items_for_user_25 import delete_digest_items_db, find_recent_notification_by_image_key_db, get_active_user_chat_ids_db, get_digest_items_for_user_db, get_last_user_notification_db, get_users_with_pending_digests_db, pop_digest_items_for_user_db, update_user_limits_db, update_user_quiet_hours_db
from hunter.core.db_parts.database_resolve_access_request_atomic_23 import alerts_sent_in_past_hour_for_user_db, count_recent_add_attempts_db, enqueue_digest_item_db, get_alerts_by_tier_24h_db, get_requests_by_tier_24h_db, get_user_feedback_counts_db, get_user_findings_7d_db, resolve_access_request_atomic_db


class _DatabaseApi02:
        async def create_invite(
            self,
            token_hash: str,
            label: str | None,
            tier: str,
            created_by: int,
            expires_at: str,
            max_uses: int = 1,
        ) -> int:
            return await create_invite_db(
                self.connect, token_hash, label, tier, created_by, expires_at, max_uses
            )

        async def get_invite_by_hash(self, token_hash: str) -> dict | None:
            return await get_invite_by_hash_db(self.connect, token_hash)

        async def get_invite_by_id(self, invite_id: int) -> dict | None:
            return await get_invite_by_id_db(self.connect, invite_id)

        async def consume_invite_atomic(self, token_hash: str, now_str: str) -> bool:
            """Atomically claim and mark an invite as used.
            Returns True if claimed successfully, False if already used, expired, or revoked.
            """
            return await consume_invite_atomic_db(self.connect, token_hash, now_str)

        async def get_active_invites(self, created_by: int | None = None) -> list[dict]:
            return await get_active_invites_db(self.connect, created_by)

        async def get_active_invites_count(self, created_by: int | None = None) -> int:
            return await get_active_invites_count_db(self.connect, created_by)

        async def revoke_invite(self, invite_id: int) -> bool:
            return await revoke_invite_db(self.connect, invite_id)

        async def create_access_request(
            self,
            telegram_user_id: int,
            username: str | None,
            display_name: str | None,
        ) -> int:
            return await create_access_request_db(
                self.connect, telegram_user_id, username, display_name
            )

        async def get_access_request(self, request_id: int) -> dict | None:
            return await get_access_request_db(self.connect, request_id)

        async def get_recent_access_request_time(self, telegram_user_id: int) -> str | None:
            return await get_recent_access_request_time_db(self.connect, telegram_user_id)

        async def get_rejected_access_requests_count(self, telegram_user_id: int) -> int:
            return await get_rejected_access_requests_count_db(self.connect, telegram_user_id)

        async def get_access_requests_last_hour_count(self) -> int:
            return await get_access_requests_last_hour_count_db(self.connect)

        async def get_pending_access_requests_count(self) -> int:
            return await get_pending_access_requests_count_db(self.connect)

        async def resolve_access_request_atomic(
            self,
            request_id: int,
            status: str,
            decided_by: int,
        ) -> bool:
            """Atomically resolve a pending access request.
            Returns True if transitioned from 'pending' to target status, False if already resolved.
            """
            return await resolve_access_request_atomic_db(
                self.connect, request_id, status, decided_by
            )

        async def get_user_findings_7d(self, user_id: int) -> int:
            """Returns delivered alerts count in the last 7 days for recipient_user_id."""
            return await get_user_findings_7d_db(self.connect, user_id)

        async def get_user_feedback_counts(self, user_id: int) -> tuple[int, int]:
            """Returns (thumbs_up, thumbs_down) feedback counts for a user."""
            return await get_user_feedback_counts_db(self.connect, user_id)

        async def get_requests_by_tier_24h(self) -> dict[str, int]:
            """Returns request counts aggregated by user tier over the past 24 hours."""
            return await get_requests_by_tier_24h_db(self.connect)

        async def get_alerts_by_tier_24h(self) -> dict[str, int]:
            """Returns delivered alert counts aggregated by user tier over the past 24 hours."""
            return await get_alerts_by_tier_24h_db(self.connect)

        async def alerts_sent_in_past_hour_for_user(self, user_id: int) -> int:
            """Returns count of alerts delivered to user in the past 1 hour."""
            return await alerts_sent_in_past_hour_for_user_db(self.connect, user_id)

        async def count_recent_add_attempts(self, user_id: int | None = None, hours: int = 24) -> int:
            """Counts add attempts. If user_id is None, counts across all non-owners."""
            return await count_recent_add_attempts_db(self.connect, user_id, hours)

        async def enqueue_digest_item(
            self,
            user_id: int,
            avito_id: str,
            payload: str,
            reason: str,
        ) -> None:
            """Enqueues an alert for digest delivery (quiet_hours or hourly_cap)."""
            await enqueue_digest_item_db(self.connect, user_id, avito_id, payload, reason)

        async def get_digest_items_for_user(
            self,
            user_id: int,
            reason: str | None = None,
        ) -> list[dict[str, Any]]:
            """Retrieves pending digest items for a user without deleting them."""
            return await get_digest_items_for_user_db(self.connect, user_id, reason)

        async def delete_digest_items(self, item_ids: list[int]) -> None:
            """Deletes specific confirmed digest items by IDs."""
            await delete_digest_items_db(self.connect, item_ids)

        async def pop_digest_items_for_user(
            self,
            user_id: int,
            reason: str | None = None,
        ) -> list[dict[str, Any]]:
            """Atomically retrieves and clears digest items for a user."""
            return await pop_digest_items_for_user_db(self.connect, user_id, reason)

        async def get_users_with_pending_digests(self, reason: str | None = None) -> list[int]:
            """Returns list of distinct user_ids having pending items in digest_queue."""
            return await get_users_with_pending_digests_db(self.connect, reason)

        async def update_user_limits(
            self,
            user_id: int,
            max_products: int | None = None,
            min_interval_minutes: int | None = None,
        ) -> None:
            """Updates max_products and/or min_interval_minutes for a user."""
            writer = getattr(self, "_public_writer", None)
            if writer is not None:
                if max_products is None and min_interval_minutes is None:
                    return
                async def update(conn):
                    await conn.execute(
                        "UPDATE users SET max_products = COALESCE(?, max_products), "
                        "min_interval_minutes = COALESCE(?, min_interval_minutes) WHERE id = ?",
                        (max_products, min_interval_minutes, user_id),
                    )
                await writer.submit(update)
                return
            await update_user_limits_db(
                self.connect, user_id, max_products, min_interval_minutes
            )

        async def update_user_quiet_hours(
            self,
            user_id: int,
            start_hour: int | None,
            end_hour: int | None,
        ) -> None:
            """Sets quiet hours (e.g. 23 to 8) or disables them if None."""
            writer = getattr(self, "_public_writer", None)
            if writer is not None:
                async def update(conn):
                    await conn.execute(
                        "UPDATE users SET quiet_start = ?, quiet_end = ? WHERE id = ?",
                        (start_hour, end_hour, user_id),
                    )
                await writer.submit(update)
                return
            await update_user_quiet_hours_db(self.connect, user_id, start_hour, end_hour)

        async def get_active_user_chat_ids(self, tier: str | None = None) -> list[int]:
            """Returns chat IDs of all active users, optionally filtered by tier."""
            return await get_active_user_chat_ids_db(self.connect, tier)

        async def get_last_user_notification(self, user_id: int, avito_id: str) -> dict[str, Any] | None:
            """Returns the most recent notification for (user_id, avito_id)."""
            return await get_last_user_notification_db(self.connect, user_id, avito_id)

        async def find_recent_notification_by_image_key(
            self,
            image_key: str,
            *,
            recipient_user_id: int | None = None,
            days: int = 14,
        ) -> dict[str, Any] | None:
            """Returns the most recent sent notification matching image_key within N days."""
            return await find_recent_notification_by_image_key_db(
                self.connect,
                image_key,
                recipient_user_id=recipient_user_id,
                days=days,
            )
