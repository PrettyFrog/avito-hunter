"""Unit tests for SERP-only price observation and coverage contracts (T-507)."""

from hunter.core.models import PriceCoverageContractDTO
from hunter.domain.observations import (
    PriceCoverageMode,
    PriceObservationRecord,
    PriceState,
)


def test_old_lot_outside_serp_does_not_get_fake_price_change() -> None:
    rec = PriceObservationRecord(
        avito_id="lot_old_1",
        known_price=45000,
        price_state=PriceState.ACTIVE,
        coverage_mode=PriceCoverageMode.SERP_ONLY,
        is_observed_in_serp=True,
        revision=1,
    )
    # Lot drops out of current SERP window
    absent_rec = rec.apply_absence_from_serp(observed_at="2026-10-23T12:00:00Z")

    assert absent_rec.avito_id == "lot_old_1"
    assert absent_rec.known_price == 45000  # Known price is NOT erased
    assert absent_rec.revision == 1  # No revision bump
    assert absent_rec.is_observed_in_serp is False
    assert absent_rec.price_state == PriceState.ACTIVE


def test_known_price_and_revision_never_erased_on_missing() -> None:
    rec = PriceObservationRecord(avito_id="lot_old_2", known_price=120000)
    for ts in ["t1", "t2", "t3"]:
        rec = rec.apply_absence_from_serp(observed_at=ts)
        assert rec.known_price == 120000
        assert rec.revision == 1
        assert rec.is_observed_in_serp is False


def test_retired_and_paused_coverage_reflected_in_status() -> None:
    rec = PriceObservationRecord(avito_id="lot_paused_1", known_price=50000)
    assert rec.coverage_mode == PriceCoverageMode.SERP_ONLY

    paused = rec.with_coverage_mode(PriceCoverageMode.PAUSED)
    assert paused.coverage_mode == PriceCoverageMode.PAUSED
    assert paused.known_price == 50000

    retired = rec.with_coverage_mode(PriceCoverageMode.RETIRED)
    assert retired.coverage_mode == PriceCoverageMode.RETIRED
    assert retired.known_price == 50000


def test_a_b_a_price_sequence_preserves_identities_and_revisions() -> None:
    rec = PriceObservationRecord(avito_id="lot_aba", known_price=20000, revision=1)

    # A -> B (20000 -> 15000)
    rec_b, ev_b = rec.apply_serp_observation(new_price=15000, observed_at="2026-10-23T12:05:00Z")
    assert ev_b == "PRICE_CHANGED"
    assert rec_b.known_price == 15000
    assert rec_b.revision == 2

    # B -> A (15000 -> 20000)
    rec_a2, ev_a2 = rec_b.apply_serp_observation(
        new_price=20000, observed_at="2026-10-23T12:10:00Z"
    )
    assert ev_a2 == "PRICE_CHANGED"
    assert rec_a2.known_price == 20000
    assert rec_a2.revision == 3

    # Re-observing same price in SERP emits no new event
    rec_same, ev_same = rec_a2.apply_serp_observation(
        new_price=20000, observed_at="2026-10-23T12:15:00Z"
    )
    assert ev_same is None
    assert rec_same.known_price == 20000
    assert rec_same.revision == 3


def test_public_dto_contract_defaults_to_serp_only() -> None:
    dto = PriceCoverageContractDTO(avito_id="lot_dto_1", known_price=30000)
    assert dto.coverage_mode == "serp_only"
    assert dto.is_observed_in_serp is True
    assert dto.revision == 1

    d = dto.model_dump()
    assert d["coverage_mode"] == "serp_only"
    assert d["known_price"] == 30000
    assert d["is_observed_in_serp"] is True
