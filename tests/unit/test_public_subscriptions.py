"""Tests for public subscription service and query URL validation (S4 / T-473)."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.application.subscriptions import SubscriptionService
from hunter.domain.query_url import (
    ConflictingPriceError,
    InvalidSchemeError,
    UntrustedHostError,
    UserinfoNotAllowedError,
    validate_and_canonicalize_query_url,
)
from hunter.storage.subscriptions import SubscriptionActor, SubscriptionRecord


def test_exact_pmax_and_no_headroom():
    canon = validate_and_canonicalize_query_url("https://m.avito.ru/moskva?q=sony", 15000)
    assert (
        "pmax=15000" in canon
        and "18000" not in canon
        and "www.avito.ru" in canon
        and "s=104" in canon
    )


def test_conflicting_pmax_and_f_price_rejected():
    with pytest.raises(ConflictingPriceError):
        validate_and_canonicalize_query_url("https://www.avito.ru/moskva?pmax=12000&q=sony", 15000)

    f_json = json.dumps({"from": 5000, "to": 10000}).encode("utf-8")
    f_val = base64.b64encode(b"\x00\x1f" + f_json).decode("ascii")
    with pytest.raises(ConflictingPriceError):
        validate_and_canonicalize_query_url(f"https://www.avito.ru/moskva?f={f_val}&q=sony", 15000)


def test_matching_f_price_and_opaque_preserved():
    f_json = json.dumps({"from": 5000, "to": 15000, "brand": "Sony", "model": 123}).encode("utf-8")
    f_val = base64.b64encode(b"\x00\x2f" + f_json).decode("ascii")
    canon = validate_and_canonicalize_query_url(
        f"https://www.avito.ru/moskva?f={f_val}&pmin=5000&p=2&utm_source=tg&q=sony", 15000
    )
    assert (
        "pmax=15000" in canon
        and "pmin=" not in canon
        and "p=2" not in canon
        and "utm_source" not in canon
        and "f=" in canon
    )


def test_security_rejections_localhost_userinfo_wrong_host():
    for bad in [
        "http://www.avito.ru/s",
        "https://u:p@www.avito.ru/s",
        "https://localhost/s",
        "https://127.0.0.1/s",
        "https://avito.ru.evil.com/s",
        "https://google.com/s",
    ]:
        with pytest.raises((InvalidSchemeError, UserinfoNotAllowedError, UntrustedHostError)):
            validate_and_canonicalize_query_url(bad, 10000)


@pytest.mark.asyncio
async def test_subscription_service_add_and_zero_llm():
    repo = MagicMock(
        create=AsyncMock(
            return_value=SubscriptionRecord("s1", 101, "q1", 20000, "broken,parts", True, 1)
        )
    )
    res = await SubscriptionService(repo).add_subscription(
        101, "https://www.avito.ru/rossiya?q=macbook", 20000, ["Broken", "Parts "], sub_id="s1"
    )
    assert res.id == "s1"
    repo.create.assert_called_once()
    kw = repo.create.call_args.kwargs
    assert (
        kw["actor"] == SubscriptionActor(user_id=101)
        and kw["pmax"] == 20000
        and kw["words"] == "broken,parts"
        and "pmax=20000" in kw["url"]
    )


def test_f_param_plain_json_round_trip():
    from hunter.domain.query_url import sanitize_f_param

    orig = json.dumps({"to": 50000, "from": 10000, "brand": "Apple"}).encode("utf-8")
    f_val = base64.b64encode(orig).decode("ascii")
    sanitized = sanitize_f_param(f_val, 50000)
    decoded = json.loads(base64.b64decode(sanitized + "==").decode("utf-8"))
    assert decoded["to"] == 50000
    assert "from" not in decoded
    assert decoded["brand"] == "Apple"


def test_f_param_protobuf_varint_multibyte_length():
    from hunter.domain.query_url import sanitize_f_param

    payload_dict = {"to": 50000, "from": 10000, "padding": "x" * 127}
    j_bytes = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    assert len(j_bytes) == 165
    # tag 1, wire type 2 = 0x0a; 165 varint = 0xa5, 0x01
    raw = b"\x0a\xa5\x01" + j_bytes
    f_val = base64.b64encode(raw).decode("ascii")

    sanitized = sanitize_f_param(f_val, 50000)
    decoded_raw = base64.b64decode(sanitized + "==")

    # Check valid framing
    assert decoded_raw[0] == 0x0A
    # decode varint
    res, shift, pos = 0, 0, 1
    while pos < len(decoded_raw):
        b = decoded_raw[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7

    assert res == len(decoded_raw[pos:]), "Declared length must match actual remaining bytes"
    parsed = json.loads(decoded_raw[pos:].decode("utf-8"))
    assert parsed["to"] == 50000
    assert "from" not in parsed
    assert parsed["padding"] == "x" * 127


def test_f_param_non_price_opaque_preserved():
    from hunter.domain.query_url import sanitize_f_param

    non_price = {"category_id": 123, "city": "moscow"}
    j_bytes = json.dumps(non_price, separators=(",", ":")).encode("utf-8")
    f_val = base64.b64encode(j_bytes).decode("ascii")
    assert sanitize_f_param(f_val, 50000) == f_val


def test_f_param_lengths_boundaries_127_128_255_256():
    from hunter.domain.query_url import _decode_varint, _encode_varint, sanitize_f_param

    for target_len in [127, 128, 255, 256]:
        base = {"to": 10000, "from": 2000, "pad": ""}
        base_len = len(json.dumps(base, separators=(",", ":")).encode("utf-8"))
        pad_len = target_len - base_len
        base["pad"] = "y" * pad_len
        j_bytes = json.dumps(base, separators=(",", ":")).encode("utf-8")
        assert len(j_bytes) == target_len

        raw = b"\x0a" + _encode_varint(len(j_bytes)) + j_bytes
        f_val = base64.b64encode(raw).decode("ascii")

        sanitized = sanitize_f_param(f_val, 10000)
        decoded = base64.b64decode(sanitized + "==")
        assert decoded[0] == 0x0A
        declared_len, pos = _decode_varint(decoded, 1)
        assert declared_len == len(decoded[pos:])
        parsed = json.loads(decoded[pos:].decode("utf-8"))
        assert parsed["to"] == 10000
        assert "from" not in parsed


@pytest.mark.asyncio
async def test_subscription_service_tenant_isolation():
    repo = MagicMock(list_for_actor=AsyncMock(return_value=[]), delete=AsyncMock())
    service = SubscriptionService(repo)
    await service.list_subscriptions(202)
    repo.list_for_actor.assert_called_once_with(SubscriptionActor(user_id=202))
    await service.delete_subscription(202, "sub_x")
    repo.delete.assert_called_once_with(SubscriptionActor(user_id=202), "sub_x")


@pytest.mark.asyncio
async def test_new_query_has_pending_bootstrap_and_first_snapshot_silent(tmp_path):
    import sqlite3
    from pathlib import Path

    from hunter.domain.observations import (
        ObservationBatch,
        ObservedListing,
        ParseOutcome,
        PriceState,
    )
    from hunter.storage.connection import StorageConfig, StorageConnectionManager
    from hunter.storage.observation_tx import execute_observation_tx
    from hunter.storage.subscriptions import SubscriptionRepository
    from hunter.storage.writer import SQLiteWriterActor

    db_path = tmp_path / "boot_test.db"
    con = sqlite3.connect(db_path)
    schema_path = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"
    con.executescript(schema_path.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        repo = SubscriptionRepository(mgr, writer)
        service = SubscriptionService(repo)
        sub = await service.add_subscription(501, "https://www.avito.ru/moskva?q=macbook", 100000)

        listing = ObservedListing(
            "lot_boot", "Macbook", 80000, PriceState.ACTIVE, "https://avito.ru/lot_boot"
        )
        batch = ObservationBatch(
            "slot_1", sub.query_id, "2026-10-23T10:00:00Z", ParseOutcome.OK, (listing,)
        )
        res = await writer.submit(lambda c: execute_observation_tx(c, batch, request_sequence=1))

        assert res.is_bootstrap is True, "First observation of new subscription must be bootstrap"
        assert res.deliveries_created == 0, "Bootstrap snapshot must create 0 deliveries"
    finally:
        await writer.stop()
        await mgr.drain_and_close()


@pytest.mark.asyncio
async def test_subscriber_attaches_existing_query_is_silent_and_new_lot_delivered(tmp_path):
    import sqlite3
    from pathlib import Path

    from hunter.domain.observations import (
        ObservationBatch,
        ObservedListing,
        ParseOutcome,
        PriceState,
    )
    from hunter.storage.connection import StorageConfig, StorageConnectionManager
    from hunter.storage.observation_tx import execute_observation_tx
    from hunter.storage.subscriptions import SubscriptionRepository
    from hunter.storage.writer import SQLiteWriterActor

    db_path = tmp_path / "attach_test.db"
    con = sqlite3.connect(db_path)
    schema_path = Path(__file__).resolve().parent.parent.parent / "src/hunter/core/schema.sql"
    con.executescript(schema_path.read_text(encoding="utf-8"))
    con.close()

    mgr = StorageConnectionManager(StorageConfig(db_path=db_path))
    writer = SQLiteWriterActor(mgr)
    await writer.start()
    try:
        repo = SubscriptionRepository(mgr, writer)
        service = SubscriptionService(repo)
        url = "https://www.avito.ru/moskva?q=iphone"

        # User 1 subscribes
        sub1 = await service.add_subscription(601, url, 100000)

        # Batch 1: bootstrap
        l1 = ObservedListing(
            "lot_1", "iPhone 13", 50000, PriceState.ACTIVE, "https://avito.ru/lot_1"
        )
        b1 = ObservationBatch(
            "slot_1", sub1.query_id, "2026-10-23T10:00:00Z", ParseOutcome.OK, (l1,)
        )
        r1 = await writer.submit(lambda c: execute_observation_tx(c, b1, request_sequence=1))
        assert r1.is_bootstrap and r1.deliveries_created == 0

        # Batch 2: new listing lot_2
        l2 = ObservedListing(
            "lot_2", "iPhone 14", 60000, PriceState.ACTIVE, "https://avito.ru/lot_2"
        )
        b2 = ObservationBatch(
            "slot_1", sub1.query_id, "2026-10-23T10:01:00Z", ParseOutcome.OK, (l1, l2)
        )
        r2 = await writer.submit(lambda c: execute_observation_tx(c, b2, request_sequence=2))
        assert r2.deliveries_created == 1

        # User 2 now attaches to existing query
        sub2 = await service.add_subscription(602, url, 100000)
        assert sub2.query_id == sub1.query_id

        # Batch 3: unchanged lots (0 new global events)
        b3 = ObservationBatch(
            "slot_1", sub1.query_id, "2026-10-23T10:02:00Z", ParseOutcome.OK, (l1, l2)
        )
        r3 = await writer.submit(lambda c: execute_observation_tx(c, b3, request_sequence=3))
        assert len(r3.events_generated) == 0, "No new events on unchanged lots"
        assert r3.deliveries_created == 0, (
            "Attaching user must not receive replay deliveries for unchanged lots"
        )

        # Batch 4: brand new lot_3
        l3 = ObservedListing(
            "lot_3", "iPhone 15", 70000, PriceState.ACTIVE, "https://avito.ru/lot_3"
        )
        b4 = ObservationBatch(
            "slot_1", sub1.query_id, "2026-10-23T10:03:00Z", ParseOutcome.OK, (l1, l2, l3)
        )
        r4 = await writer.submit(lambda c: execute_observation_tx(c, b4, request_sequence=4))
        assert len(r4.events_generated) == 1
        assert r4.deliveries_created == 2, "Both users must receive delivery for new lot_3"
    finally:
        await writer.stop()
        await mgr.drain_and_close()
