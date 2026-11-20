"""Application service for ingesting observation batches."""

from __future__ import annotations

from dataclasses import dataclass

from hunter.application.ports import Clock, ObservationStore
from hunter.domain.observations import ObservationBatch, ParseOutcome


@dataclass(frozen=True)
class IngestResult:
    """Result of batch ingestion attempt."""

    accepted: bool
    reason: str
    item_count: int = 0


class IngestService:
    """Ingest coordinator validating and routing batches to storage."""

    def __init__(self, clock: Clock, store: ObservationStore) -> None:
        self._clock = clock
        self._store = store

    async def ingest_batch(self, batch: ObservationBatch) -> IngestResult:
        """Validate batch DTO and submit to storage if valid and outcome is OK."""
        if not isinstance(batch, ObservationBatch):
            return IngestResult(False, "Payload is not an ObservationBatch instance", 0)

        if batch.outcome != ParseOutcome.OK:
            return IngestResult(False, f"Non-OK outcome: {batch.outcome.value}", 0)

        for item in batch.listings:
            if not item.avito_id or not item.url:
                return IngestResult(False, "Listing missing avito_id or url", 0)

        count = await self._store.submit_batch(batch)
        return IngestResult(True, "OK", count)
