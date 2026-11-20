"""Additive price history v2 with sequential revision identity (S3 / T-469)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PriceRevision:
    listing_id: str
    revision: int
    price: int
    ts: str
    previous_price: int | None = None


class PriceHistoryRepository:
    """Manages additive price history v2 with revision identity and legacy migration."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._schema_ensured = False

    async def ensure_schema(self) -> None:
        if self._schema_ensured:
            return
        async with self.db.write_lock:
            conn = await self.db.connect()
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history_v2 (
                    listing_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    price INTEGER NOT NULL, ts TEXT NOT NULL, previous_price INTEGER,
                    PRIMARY KEY (listing_id, revision)
                )
                """
            )
            await conn.commit()
            self._schema_ensured = True

    async def record_price(
        self, listing_id: str, price: int, ts: str, previous_price: int | None = None
    ) -> PriceRevision:
        await self.ensure_schema()
        async with self.db.write_lock:
            conn = await self.db.connect()
            cur = await conn.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM price_history_v2 WHERE listing_id = ?", (listing_id,)
            )
            max_rev = (await cur.fetchone())[0]
            new_rev = max_rev + 1
            await conn.execute(
                "INSERT INTO price_history_v2 (listing_id, revision, price, ts, previous_price) VALUES (?, ?, ?, ?, ?)",
                (listing_id, new_rev, price, ts, previous_price),
            )
            await conn.commit()
            return PriceRevision(listing_id, new_rev, price, ts, previous_price)

    async def get_history(self, listing_id: str, use_v2: bool = True) -> list[PriceRevision]:
        await self.ensure_schema()
        conn = await self.db.connect()
        if use_v2:
            cur = await conn.execute(
                "SELECT listing_id, revision, price, ts, previous_price FROM price_history_v2 "
                "WHERE listing_id = ? ORDER BY revision ASC", (listing_id,)
            )
            return [PriceRevision(r["listing_id"], r["revision"], r["price"], r["ts"], r["previous_price"]) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT listing_id, ts, price FROM price_history WHERE listing_id = ? ORDER BY ts ASC, rowid ASC",
            (listing_id,),
        )
        res: list[PriceRevision] = []
        prev_p = None
        for idx, r in enumerate(await cur.fetchall(), start=1):
            res.append(PriceRevision(r["listing_id"], idx, r["price"], r["ts"], prev_p))
            prev_p = r["price"]
        return res

    async def import_legacy_rows(self) -> int:
        await self.ensure_schema()
        async with self.db.write_lock:
            conn = await self.db.connect()
            cur = await conn.execute(
                "SELECT listing_id, ts, price FROM price_history ORDER BY listing_id ASC, ts ASC, rowid ASC"
            )
            legacy_rows = await cur.fetchall()
            if not legacy_rows:
                return 0
            cur_existing = await conn.execute("SELECT DISTINCT listing_id FROM price_history_v2")
            migrated = {r[0] for r in await cur_existing.fetchall()}
            imported, cur_listing, cur_rev, prev_p = 0, None, 0, None
            for row in legacy_rows:
                lid = row["listing_id"]
                if lid in migrated:
                    continue
                if lid != cur_listing:
                    cur_listing, cur_rev, prev_p = lid, 0, None
                cur_rev += 1
                await conn.execute(
                    "INSERT INTO price_history_v2 VALUES (?, ?, ?, ?, ?)",
                    (lid, cur_rev, row["price"], row["ts"], prev_p),
                )
                prev_p = row["price"]
                imported += 1
            await conn.commit()
            return imported
