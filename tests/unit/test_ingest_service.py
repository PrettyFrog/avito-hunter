"""Unit tests for Application Ports and IngestService (T-472)."""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hunter.application.ingest import IngestService
from hunter.domain.observations import (
    ObservationBatch,
    ObservedListing,
    ParseOutcome,
    PriceState,
)


class FakeClock:
    def __init__(self) -> None:
        self._current = datetime(2026, 10, 23, 10, 0, 0, tzinfo=UTC)

    def now_utc(self) -> datetime:
        return self._current

    def timestamp(self) -> float:
        return self._current.timestamp()

    def isoformat(self) -> str:
        return self._current.isoformat()

    async def sleep(self, seconds: float) -> None:
        pass


class FakeObservationStore:
    def __init__(self) -> None:
        self.submitted_batches: list[ObservationBatch] = []

    async def submit_batch(self, batch: ObservationBatch) -> int:
        self.submitted_batches.append(batch)
        return len(batch.listings)

    async def get_latest_batch(self, query_id: str) -> ObservationBatch | None:
        return next((b for b in reversed(self.submitted_batches) if b.query_id == query_id), None)


def _make_batch(outcome: ParseOutcome = ParseOutcome.OK) -> ObservationBatch:
    now_iso = "2026-10-23T10:00:00Z"
    listings = (
        (ObservedListing("lot_1", "MacBook Pro", 110000, PriceState.ACTIVE, "https://avito.ru/1"),)
        if outcome == ParseOutcome.OK
        else ()
    )
    return ObservationBatch("slot_alpha", "query_macbook", now_iso, outcome, listings)


@pytest.mark.asyncio
async def test_ingest_service_success() -> None:
    clock, store = FakeClock(), FakeObservationStore()
    service = IngestService(clock=clock, store=store)
    batch = _make_batch(ParseOutcome.OK)
    res = await service.ingest_batch(batch)
    assert res.accepted and res.reason == "OK" and res.item_count == 1
    assert store.submitted_batches == [batch]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ParseOutcome.EMPTY,
        ParseOutcome.CUTOFF,
        ParseOutcome.CAPTCHA,
        ParseOutcome.BLOCKED_403,
        ParseOutcome.RATE_LIMIT_429,
        ParseOutcome.NETWORK_ERROR,
        ParseOutcome.PARSE_ERROR,
    ],
)
async def test_ingest_service_rejects_non_ok_outcome_without_state_changes(
    outcome: ParseOutcome,
) -> None:
    clock, store = FakeClock(), FakeObservationStore()
    service = IngestService(clock=clock, store=store)
    res = await service.ingest_batch(_make_batch(outcome))
    assert not res.accepted and outcome.value in res.reason and res.item_count == 0
    assert len(store.submitted_batches) == 0


@pytest.mark.asyncio
async def test_ingest_service_rejects_invalid_batch_payload() -> None:
    clock, store = FakeClock(), FakeObservationStore()
    service = IngestService(clock=clock, store=store)
    res = await service.ingest_batch("invalid_payload")  # type: ignore[arg-type]
    assert not res.accepted and len(store.submitted_batches) == 0


def test_domain_and_application_import_guard() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    checked = [repo_root / "src/hunter/domain", repo_root / "src/hunter/application"]
    forbidden = (
        "hunter.llm",
        "hunter.config",
        "hunter.core.db",
        "aiohttp",
        "httpx",
        "requests",
        "sqlite3",
    )
    for d in checked:
        for f in d.glob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for alias in n.names:
                        if f.name == "owner_enrichment.py" and alias.name == "hunter.llm.router":
                            continue
                        assert not any(alias.name.startswith(p) for p in forbidden), (
                            f"{f.name}: {alias.name}"
                        )
                elif isinstance(n, ast.ImportFrom) and n.module:
                    if f.name == "owner_enrichment.py" and n.module == "hunter.llm.router":
                        continue
                    assert not any(n.module.startswith(p) for p in forbidden), (
                        f"{f.name}: {n.module}"
                    )

    enrichment = ast.parse((repo_root / "src/hunter/application/owner_enrichment.py").read_text())
    get_router = next(
        n
        for n in enrichment.body
        if isinstance(n, ast.ClassDef) and n.name == "OwnerEnrichmentService"
    )
    method = next(
        n for n in get_router.body if isinstance(n, ast.FunctionDef) and n.name == "_get_router"
    )
    assert any(
        isinstance(n, ast.ImportFrom) and n.module == "hunter.llm.router" for n in ast.walk(method)
    ), "Owner-only LLM adapter must stay lazy-loaded behind the hidden gate"
