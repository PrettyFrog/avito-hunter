from __future__ import annotations
from datetime import datetime
from typing import Any
from hunter.core.db_parts.database_get_recent_shadow_overlaps_54 import get_7day_field_coverage_baseline_db, get_recent_shadow_overlaps_db, get_shadow_autostop_db, set_shadow_autostop_db
from hunter.core.db_parts.database_get_queued_digest_items_50 import bump_product_photo_checks_db, get_cached_photo_check_db, get_cached_photo_check_v2_db, log_llm_call_db, save_cached_photo_check_db, save_photo_check_db, today_product_photo_checks_db
from hunter.core.db_parts.database_save_photo_shadow_result_52 import count_today_photo_shadow_results_db, get_photo_shadow_results_db, get_shadow_fetch_stats_db, get_shadow_last_ts_db, get_shadow_today_count_db, get_shadow_total_count_db, record_shadow_fetch_db, save_photo_shadow_result_db


class _DatabaseApi06:
        async def log_llm_call(
            self,
            *,
            provider: str,
            model: str,
            call_type: str,
            product_id: str | None = None,
            avito_id: str | None = None,
            tokens_prompt: int = 0,
            tokens_completion: int = 0,
            tokens_total: int = 0,
            latency_ms: int = 0,
            status: str = "ok",
            error_msg: str | None = None,
        ) -> None:
            conn = await self.connect()
            await log_llm_call_db(
                conn,
                provider=provider,
                model=model,
                call_type=call_type,
                product_id=product_id,
                avito_id=avito_id,
                tokens_prompt=tokens_prompt,
                tokens_completion=tokens_completion,
                tokens_total=tokens_total,
                latency_ms=latency_ms,
                status=status,
                error_msg=error_msg,
            )

        async def get_cached_photo_check(
            self,
            photo_sha256: str,
            sig_hash: str,
            method: str,
            provider: str,
        ) -> dict[str, Any] | None:
            conn = await self.connect()
            return await get_cached_photo_check_db(conn, photo_sha256, sig_hash, method, provider)

        async def save_cached_photo_check(
            self,
            photo_sha256: str,
            sig_hash: str,
            method: str,
            provider: str,
            result: dict[str, Any],
        ) -> None:
            conn = await self.connect()
            await save_cached_photo_check_db(conn, photo_sha256, sig_hash, method, provider, result)

        async def get_cached_photo_check_v2(
            self,
            photo_hash: str,
            signature_version: str,
            prompt_version: str,
            method: str,
            provider: str,
        ) -> dict[str, Any] | None:
            conn = await self.connect()
            return await get_cached_photo_check_v2_db(
                conn, photo_hash, signature_version, prompt_version, method, provider
            )

        async def save_photo_check(
            self,
            avito_id: str,
            product_id: str,
            method: str,
            photo_hash: str,
            signature_version: str,
            prompt_version: str,
            provider: str,
            model_version: str,
            result: dict[str, Any],
        ) -> None:
            conn = await self.connect()
            await save_photo_check_db(
                conn,
                avito_id,
                product_id,
                method,
                photo_hash,
                signature_version,
                prompt_version,
                provider,
                model_version,
                result,
            )

        async def today_product_photo_checks(self, product_id: str) -> int:
            return await today_product_photo_checks_db(self.get_runtime, product_id)

        async def bump_product_photo_checks(self, product_id: str) -> int:
            return await bump_product_photo_checks_db(self.get_runtime, self.set_runtime, product_id)

        async def save_photo_shadow_result(
            self,
            product_id: str,
            avito_id: str,
            result: Any,
            photo_paths: list[Any] | None = None,
        ) -> None:
            conn = await self.connect()
            await save_photo_shadow_result_db(conn, product_id, avito_id, result, photo_paths)

        async def get_photo_shadow_results(
            self,
            product_id: str,
            limit: int = 50,
        ) -> list[dict[str, Any]]:
            conn = await self.connect()
            return await get_photo_shadow_results_db(conn, product_id, limit)

        async def count_today_photo_shadow_results(self, product_id: str) -> int:
            conn = await self.connect()
            return await count_today_photo_shadow_results_db(conn, product_id)

        async def record_shadow_fetch(self, record: dict) -> int:
            conn = await self.connect()
            return await record_shadow_fetch_db(conn, record)

        async def get_shadow_fetch_stats(self, watch_id: str | None = None, limit: int = 50) -> list[dict]:
            conn = await self.connect()
            return await get_shadow_fetch_stats_db(conn, watch_id, limit)

        async def get_shadow_today_count(self, watch_id: str | None = None) -> int:
            conn = await self.connect()
            return await get_shadow_today_count_db(conn, watch_id)

        async def get_shadow_total_count(self, watch_id: str | None = None) -> int:
            conn = await self.connect()
            return await get_shadow_total_count_db(conn, watch_id)

        async def get_shadow_last_ts(self, watch_id: str | None = None) -> str | None:
            conn = await self.connect()
            return await get_shadow_last_ts_db(conn, watch_id)

        async def get_recent_shadow_overlaps(self, watch_id: str, limit: int = 2) -> list[float | None]:
            conn = await self.connect()
            return await get_recent_shadow_overlaps_db(conn, watch_id, limit)

        async def get_shadow_autostop(self) -> datetime | None:
            return await get_shadow_autostop_db(self.get_runtime)

        async def set_shadow_autostop(self, until: datetime) -> None:
            await set_shadow_autostop_db(self.set_runtime, until)

        async def get_7day_field_coverage_baseline(self) -> dict[str, float]:
            conn = await self.connect()
            return await get_7day_field_coverage_baseline_db(conn)
