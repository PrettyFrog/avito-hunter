from dataclasses import replace

import pytest

from hunter.notify.digest_delivery import format_digest_chunks
from hunter.storage.deliveries import ClaimedDelivery


def item(number: int, title: str = "Item") -> ClaimedDelivery:
    return ClaimedDelivery(
        f"d{number}",
        f"e{number}",
        1,
        "telegram",
        1,
        {"title": title, "new_price": 100 + number, "url": f"https://avito.ru/item/{number}"},
        1,
        "lease",
        "",
        3,
    )


def test_digest_chunks_preserve_each_event_and_price_change():
    changed = replace(
        item(2),
        payload={
            "title": "Changed",
            "new_price": 200,
            "old_price": 150,
            "url": "https://avito.ru/item/2",
        },
    )
    chunks = format_digest_chunks([item(1), changed])
    assert len(chunks) == 1
    assert chunks[0].delivery_ids == ("d1", "d2")
    assert "https://avito.ru/item/1" in chunks[0].text
    assert "https://avito.ru/item/2" in chunks[0].text
    assert "150 ₽" in chunks[0].text and "200 ₽" in chunks[0].text


def test_digest_splits_without_dropping_or_reordering_events():
    items = [item(n, "😀" * 200) for n in range(15)]
    chunks = format_digest_chunks(items, max_units=600)
    assert len(chunks) > 1
    assert [did for chunk in chunks for did in chunk.delivery_ids] == [d.id for d in items]
    assert all(len(chunk.text.encode("utf-16-le")) // 2 <= 600 for chunk in chunks)


@pytest.mark.parametrize(
    "other",
    [
        replace(item(2), user_id=2),
        replace(item(2), channel="vk"),
        replace(item(2), destination_version=4),
    ],
)
def test_digest_rejects_mixed_recipient_or_destination(other):
    with pytest.raises(ValueError):
        format_digest_chunks([item(1), other])


def test_digest_rejects_event_that_cannot_fit_without_dropping_link():
    too_long = replace(
        item(1), payload={"title": "x", "new_price": 1, "url": "https://x/" + "a" * 5000}
    )
    with pytest.raises(ValueError):
        format_digest_chunks([too_long])


def test_digest_rejects_duplicate_events_and_invalid_prices():
    with pytest.raises(ValueError, match="duplicate"):
        format_digest_chunks([item(1), item(1)])
    bad = replace(item(1), payload={"title": "x", "url": "https://avito.ru/1", "new_price": -1})
    with pytest.raises(ValueError, match="price"):
        format_digest_chunks([bad])
