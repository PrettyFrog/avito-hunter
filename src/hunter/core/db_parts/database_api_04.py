from __future__ import annotations
import json
from typing import Any
from hunter.core.db_parts.database_get_pending_notifications_39 import get_pending_notifications_db, get_seller_db, get_watch_hits_24h_db, latest_evaluation_db, latest_evaluations_db, latest_snapshot_db, mark_missing_db, mark_notification_expired_db, mark_notification_sent_db, market_prices_db, save_snapshot_db, update_notification_retry_db, upsert_seller_db
from hunter.core.db_parts.database_today_requests_34 import insert_new_listing_db, record_listing_price_change_db, update_existing_listing_db
from hunter.core.db_parts.database_sync_products_41 import find_duplicate_images_db, get_images_db, get_product_profile_db, save_image_db, save_product_profile_db, search_listings_fts_db, sync_products_db
from hunter.core.db_parts.database_touch_listing_37 import add_event_db, events_db, get_listing_db, notification_exists_db, record_notification_db, save_evaluation_db, save_extraction_db, touch_listing_db
from hunter.core.models import utcnow


class _DatabaseApi04:
        async def upsert_listing(self, data: dict) -> str:
            conn = await self.connect()
            now = utcnow()
            avito_id = str(data["avito_id"])
            cur = await conn.execute(
                "SELECT avito_id, price, status FROM listings WHERE avito_id = ?",
                (avito_id,),
            )
            existing = await cur.fetchone()
            params = data.get("params")
            if isinstance(params, dict):
                params = json.dumps(params, ensure_ascii=False)
            if existing is None:
                inserted = await insert_new_listing_db(
                    conn,
                    avito_id,
                    data,
                    params,
                    now,
                    self.add_event,
                )
                if inserted:
                    if data.get("price") is not None:
                        await self.price_history_repo.record_price(avito_id, data["price"], now)
                    return "new"
                cur = await conn.execute(
                    "SELECT price FROM listings WHERE avito_id = ?",
                    (avito_id,),
                )
                existing = await cur.fetchone()

            old_price = existing["price"] if existing else None
            await update_existing_listing_db(
                conn,
                avito_id,
                data,
                params,
                now,
            )
            if data.get("price") is not None and (old_price is None or data["price"] != old_price):
                await self.price_history_repo.record_price(avito_id, data["price"], now, previous_price=old_price)
            return await record_listing_price_change_db(
                conn,
                avito_id,
                data,
                old_price,
                now,
                self.add_event,
            )

        @property
        def price_history_repo(self) -> Any:
            if not hasattr(self, "_price_history_repo") or self._price_history_repo is None:
                from hunter.storage.price_history import PriceHistoryRepository

                self._price_history_repo = PriceHistoryRepository(self)
            return self._price_history_repo

        async def get_price_history(self, listing_id: str, use_v2: bool = True) -> list[Any]:
            return await self.price_history_repo.get_history(listing_id, use_v2=use_v2)

        async def touch_listing(self, avito_id: str) -> None:
            await touch_listing_db(self.connect, avito_id)

        async def add_event(self, avito_id: str, kind: str, payload: dict | None = None) -> None:
            await add_event_db(self.connect, avito_id, kind, payload)

        async def get_listing(self, avito_id: str) -> dict | None:
            return await get_listing_db(self.connect, avito_id)

        async def events(self, avito_id: str) -> list[dict]:
            return await events_db(self.connect, avito_id)

        async def save_evaluation(self, avito_id: str, data: dict) -> bool:
            return await save_evaluation_db(self.connect, avito_id, data)

        async def save_extraction(self, avito_id: str, data: dict) -> None:
            await save_extraction_db(self.connect, avito_id, data)

        async def notification_exists(self, key: str) -> bool:
            return await notification_exists_db(self.connect, key)

        async def record_notification(
            self,
            avito_id: str,
            channel: str,
            key: str,
            verdict: str,
            sent_at: str | None = None,
            status: str = "sent",
            attempts: int = 1,
            next_try_at: str | None = None,
            payload: str | None = None,
            owner_user_id: int | None = None,
            recipient_user_id: int | None = None,
            photo_key: str | None = None,
        ) -> None:
            await record_notification_db(
                self.connect,
                avito_id,
                channel,
                key,
                verdict,
                sent_at,
                status,
                attempts,
                next_try_at,
                payload,
                owner_user_id,
                recipient_user_id,
                photo_key,
            )

        async def get_pending_notifications(self, now_str: str | None = None) -> list[dict]:
            return await get_pending_notifications_db(self.connect, now_str)

        async def mark_notification_sent(self, notification_id: int, sent_at: str, channel: str | None = None) -> None:
            await mark_notification_sent_db(self.connect, notification_id, sent_at, channel)

        async def mark_notification_expired(self, notification_id: int) -> None:
            await mark_notification_expired_db(self.connect, notification_id)

        async def get_watch_hits_24h(self, watchlist_id: str) -> int:
            return await get_watch_hits_24h_db(self.connect, watchlist_id)

        async def update_notification_retry(self, notification_id: int, attempts: int, next_try_at: str) -> None:
            await update_notification_retry_db(self.connect, notification_id, attempts, next_try_at)

        async def latest_evaluations(self, limit: int = 50) -> list[dict]:
            return await latest_evaluations_db(self.connect, limit)

        async def mark_missing(self, seen_ids: set[str], watchlist_id: str) -> None:
            await mark_missing_db(self.connect, seen_ids, watchlist_id)

        async def get_seller(self, seller_id: str) -> dict | None:
            return await get_seller_db(self.connect, seller_id)

        async def upsert_seller(self, data: dict) -> None:
            await upsert_seller_db(self.connect, data)

        async def latest_evaluation(self, avito_id: str) -> dict | None:
            return await latest_evaluation_db(self.connect, avito_id)

        async def market_prices(self, model_id: str) -> list[int]:
            return await market_prices_db(self.connect, model_id)

        async def save_snapshot(self, model_id: str, stats: dict, kind: str = "ask") -> None:
            await save_snapshot_db(self.connect, model_id, stats, kind)

        async def latest_snapshot(self, model_id: str) -> dict | None:
            return await latest_snapshot_db(self.connect, model_id)

        async def sync_products(self, catalog_dict: dict[str, dict]) -> None:
            await sync_products_db(self.connect, catalog_dict)

        async def search_listings_fts(self, query: str, limit: int = 50) -> list[dict]:
            return await search_listings_fts_db(self.connect, query, limit)

        async def save_image(
            self, avito_id: str, url: str, local_path: str | None = None, phash: str | None = None
        ) -> None:
            await save_image_db(self.connect, avito_id, url, local_path, phash)

        async def get_images(self, avito_id: str) -> list[dict]:
            return await get_images_db(self.connect, avito_id)

        async def find_duplicate_images(
            self, phash: str, exclude_avito_id: str, max_distance: int = 4
        ) -> list[dict]:
            return await find_duplicate_images_db(self.connect, phash, exclude_avito_id, max_distance)

        async def get_product_profile(self, model_id: str) -> dict[str, Any] | None:
            return await get_product_profile_db(self.connect, model_id)

        async def save_product_profile(
            self,
            model_id: str,
            category: str,
            canonical_name: str,
            brand: str,
            profile: dict[str, Any],
        ) -> None:
            await save_product_profile_db(self.connect, model_id, category, canonical_name, brand, profile)
