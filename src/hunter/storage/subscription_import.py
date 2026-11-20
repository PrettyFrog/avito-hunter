"""YAML product profiles to SQLite subscription importer with checkpointing (S3 / T-470)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
from typing import Any
import aiosqlite
import yaml

from hunter.core.product import ProductProfile, is_tombstoned
from hunter.storage.connection import StorageConnectionManager
from hunter.storage.writer import SQLiteWriterActor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportReport:
    imported_count: int
    skipped_count: int
    error_count: int
    checkpoint_cursor: str
    profiles_hash: str
    errors: tuple[str, ...]


class SubscriptionImporter:
    """Imports legacy YAML product profiles into v2 queries and subscriptions."""

    def __init__(
        self, mgr: StorageConnectionManager, writer: SQLiteWriterActor, batch_size: int = 25
    ) -> None:
        self.mgr = mgr
        self.writer = writer
        self.batch_size = batch_size

    async def get_checkpoint(self) -> str:
        reader = await self.mgr.open_read_connection()
        try:
            cur = await reader.execute(
                "SELECT value FROM runtime WHERE key = 'yaml_sub_import_cursor'"
            )
            row = await cur.fetchone()
            return str(row[0]) if row and row[0] else ""
        finally:
            await self.mgr.close_reader(reader)

    async def set_checkpoint(self, cursor: str) -> None:
        async def _tx(c: aiosqlite.Connection) -> None:
            await c.execute(
                "INSERT OR REPLACE INTO runtime (key, value) VALUES ('yaml_sub_import_cursor', ?)",
                (cursor,),
            )

        await self.writer.submit(_tx)

    async def _import_batch(
        self, items: list[tuple[str, int, str, int, str, int, str]], cursor: str
    ) -> None:
        async def _tx(c: aiosqlite.Connection) -> None:
            for sub_id, uid, qid, max_p, words, enabled, url in items:
                cur = await c.execute("SELECT id FROM queries WHERE canonical_url = ?", (url,))
                row = await cur.fetchone()
                actual_qid = str(row[0]) if row else qid
                if not row:
                    await c.execute(
                        "INSERT INTO queries (id, canonical_url, bootstrap_state) VALUES (?, ?, 'ready')",
                        (qid, url),
                    )
                await c.execute(
                    """
                    INSERT INTO subscriptions (id, user_id, query_id, max_price, manual_stopwords, enabled, version)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET query_id = excluded.query_id, max_price = excluded.max_price, manual_stopwords = excluded.manual_stopwords, enabled = excluded.enabled
                    """,
                    (sub_id, uid, actual_qid, max_p, words, enabled),
                )
            await c.execute(
                "INSERT OR REPLACE INTO runtime (key, value) VALUES ('yaml_sub_import_cursor', ?)",
                (cursor,),
            )

        await self.writer.submit(_tx)

    async def import_from_directory(self, dir_path: Path, resume: bool = True) -> ImportReport:
        files = sorted(dir_path.glob("*.yaml")) + sorted(dir_path.glob("*.yml"))
        cursor = await self.get_checkpoint() if resume else ""
        hasher = hashlib.sha256()

        imported = skipped = errors_count = 0
        err_list: list[str] = []
        batch: list[tuple[str, int, str, int, str, int, str]] = []
        last_file_stem = cursor

        for f in files:
            hasher.update(f.name.encode("utf-8"))
            stem = f.stem
            if cursor and stem <= cursor:
                skipped += 1
                continue
            if is_tombstoned(stem):
                skipped += 1
                continue
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                if "id" not in data:
                    data["id"] = stem
                prof = ProductProfile.model_validate(data)
            except Exception as exc:
                errors_count += 1
                err_list.append(f"{f.name}: {exc}")
                continue

            uid = prof.owner_user_id if prof.owner_user_id is not None else 1
            enabled = 1 if prof.mode == "live" and not prof.paused_reason else 0
            words = ",".join(prof.red_flags_extra)
            batch.append(
                (prof.id, uid, f"q_{prof.id}", prof.max_price, words, enabled, prof.search_url)
            )
            imported += 1
            last_file_stem = stem

            if len(batch) >= self.batch_size:
                await self._import_batch(batch, last_file_stem)
                batch.clear()

        if batch:
            await self._import_batch(batch, last_file_stem)

        return ImportReport(
            imported,
            skipped,
            errors_count,
            last_file_stem,
            hasher.hexdigest()[:16],
            tuple(err_list),
        )
