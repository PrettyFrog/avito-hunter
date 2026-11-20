from __future__ import annotations
from typing import Any
from hunter.core.db_parts.database_check_p90_spike_32 import bump_metric_db, check_p90_spike_db, clear_all_pauses_db, delete_pending_product_db, get_feedback_stats_db, get_pending_product_db, is_paused_db, record_feedback_entry_db, save_pending_product_db, set_listing_status_db
from hunter.core.db_parts.database_get_daily_cap_with_source_28 import get_daily_cap_with_source_db, get_night_enabled_db, get_night_enabled_with_source_db, get_recent_memory_log_db, get_rolling_24h_requests_db, get_rolling_48h_signals_db, log_memory_db, record_daily_health_db, set_daily_cap_db, set_night_enabled_db
from hunter.core.db_parts.database_pop_digest_items_for_user_25 import get_daily_cap_db, get_recent_notification_by_fingerprint_db, notification_exists_by_fingerprint_db
from hunter.core.db_parts.database_today_requests_34 import find_implausible_prices_db, fix_implausible_prices_db, get_daily_metrics_db, get_metric_today_db, record_search_metrics_db, today_requests_db


class _DatabaseApi03:
        async def get_recent_notification_by_fingerprint(
            self,
            fingerprint: str,
            *,
            current_avito_id: str | None = None,
            watch_id: str | None = None,
            recipient_user_id: int | None = None,
            within_hours: int = 48,
        ) -> dict[str, Any] | None:
            """Returns the most recent sent notification for an item with the same fingerprint within N hours."""
            return await get_recent_notification_by_fingerprint_db(
                self.connect,
                fingerprint,
                current_avito_id=current_avito_id,
                watch_id=watch_id,
                recipient_user_id=recipient_user_id,
                within_hours=within_hours,
            )

        async def notification_exists_by_fingerprint(
            self,
            fingerprint: str,
            *,
            current_avito_id: str | None = None,
            watch_id: str | None = None,
            recipient_user_id: int | None = None,
            within_hours: int = 48,
        ) -> bool:
            """Returns True if a notification for an item with the same fingerprint was already sent within N hours."""
            return await notification_exists_by_fingerprint_db(
                self.get_recent_notification_by_fingerprint,
                fingerprint,
                current_avito_id=current_avito_id,
                watch_id=watch_id,
                recipient_user_id=recipient_user_id,
                within_hours=within_hours,
            )

        async def get_daily_cap(self) -> int:
            return await get_daily_cap_db(self.get_daily_cap_with_source)

        async def get_daily_cap_with_source(self) -> tuple[int, str]:
            return await get_daily_cap_with_source_db(self.get_runtime)

        async def set_daily_cap(self, cap: int) -> None:
            await set_daily_cap_db(self.set_runtime, cap)

        async def get_night_enabled_with_source(self) -> tuple[bool, str]:
            return await get_night_enabled_with_source_db(self.get_runtime)

        async def get_night_enabled(self) -> bool:
            return await get_night_enabled_db(self.get_night_enabled_with_source)

        async def set_night_enabled(self, enabled: bool | None) -> None:
            await set_night_enabled_db(self.connect, self.set_runtime, enabled)

        async def log_memory(
            self,
            python_rss_mb: float,
            node_rss_mb: float,
            chromium_rss_mb: float,
            total_rss_mb: float,
            ts: str | None = None,
        ) -> None:
            await log_memory_db(
                self.connect,
                python_rss_mb,
                node_rss_mb,
                chromium_rss_mb,
                total_rss_mb,
                ts,
            )

        async def get_recent_memory_log(self, hours: int = 12) -> list[dict[str, Any]]:
            return await get_recent_memory_log_db(self.connect, hours)

        async def get_rolling_24h_requests(self) -> int:
            return await get_rolling_24h_requests_db(self.connect)

        async def get_rolling_48h_signals(self) -> dict[str, int]:
            return await get_rolling_48h_signals_db(self.connect)

        async def record_daily_health(self, target_date: str | None = None) -> dict[str, Any]:
            """Calculates and persists daily quality and health signals in health_daily."""
            return await record_daily_health_db(self.connect, target_date)

        async def check_p90_spike(self, target_date: str | None = None) -> tuple[bool, float, float]:
            """Checks if today's search latency p90 doubled day-over-day."""
            return await check_p90_spike_db(self.connect, target_date)

        async def save_pending_product(
            self,
            chat_id: int,
            step: str,
            payload: dict[str, Any],
            ttl_hours: int = 24,
            owner_user_id: int | None = None,
        ) -> None:
            """Stores pending product addition dialog state with 24h TTL (PR-3.5 B)."""
            await save_pending_product_db(
                self.connect, chat_id, step, payload, ttl_hours, owner_user_id
            )

        async def get_pending_product(self, chat_id: int) -> dict[str, Any] | None:
            """Retrieves active pending product addition dialog state if not expired."""
            return await get_pending_product_db(self.connect, chat_id)

        async def delete_pending_product(self, chat_id: int) -> None:
            """Deletes pending product addition dialog state."""
            await delete_pending_product_db(self.connect, chat_id)

        async def record_feedback_entry(
            self,
            avito_id: str,
            feedback: str,
            product_id: str | None = None,
            owner_user_id: int | None = None,
        ) -> None:
            """Records user feedback button event into feedback table (PR-3.5 C)."""
            await record_feedback_entry_db(
                self.connect, avito_id, feedback, product_id, owner_user_id
            )

        async def get_feedback_stats(self, product_id: str | None = None) -> dict[str, Any]:
            """Calculates feedback statistics and precision (PR-3.5 C/D)."""
            return await get_feedback_stats_db(self.connect, product_id)

        async def is_paused(self) -> bool:
            return await is_paused_db(self.get_runtime, self.set_runtime)

        async def clear_all_pauses(self) -> None:
            """Clear only watch-specific canary pause state.

            Preserves global circuit_until, paused, and captcha flags so restarts
            during rate-limits or manual pauses do not bypass safety protections.
            """
            await clear_all_pauses_db(self.connect)

        async def set_listing_status(self, avito_id: str, status: str) -> None:
            await set_listing_status_db(self.connect, avito_id, status)

        async def bump_metric(self, field: str, n: int = 1) -> None:
            await bump_metric_db(self.connect, field, n)

        async def today_requests(self) -> int:
            return await today_requests_db(self.connect)

        async def get_metric_today(self, field: str) -> int:
            return await get_metric_today_db(self.connect, field)

        async def record_search_metrics(
            self,
            hits_total: int,
            hits_no_price: int,
            anchor_fallback: int = 0,
        ) -> None:
            await record_search_metrics_db(
                self.connect,
                hits_total,
                hits_no_price,
                anchor_fallback,
            )

        async def get_daily_metrics(self, day: str | None = None) -> dict | None:
            return await get_daily_metrics_db(self.connect, day)

        async def find_implausible_prices(self, threshold: int = 1_000_000) -> list[dict]:
            return await find_implausible_prices_db(self.connect, threshold)

        async def fix_implausible_prices(self, threshold: int = 1_000_000) -> int:
            return await fix_implausible_prices_db(self.connect, threshold)
