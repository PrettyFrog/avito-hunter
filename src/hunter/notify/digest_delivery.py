"""Pure, bounded formatting for tenant-isolated Telegram digest chunks."""

from dataclasses import dataclass

from hunter.notify.telegram_delivery import format_delivery_text
from hunter.storage.deliveries import ClaimedDelivery


@dataclass(frozen=True, slots=True)
class DigestChunk:
    delivery_ids: tuple[str, ...]
    text: str


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _format_item(delivery: ClaimedDelivery) -> str:
    payload = delivery.payload
    title, url = payload.get("title", ""), payload.get("url")
    price, old_price = payload.get("new_price", payload.get("price")), payload.get("old_price")
    if not isinstance(title, str) or not isinstance(url, str) or not url:
        raise ValueError("Digest event lacks a valid title or URL")
    if type(price) is not int or price < 0:
        raise ValueError("Digest event lacks a valid price")
    if old_price is not None and (type(old_price) is not int or old_price < 0):
        raise ValueError("Digest event has an invalid previous price")
    return format_delivery_text(
        title[:300], price, url, old_price, avito_id=payload.get("avito_id")
    )


def format_digest_chunks(
    deliveries: list[ClaimedDelivery], max_units: int = 3900
) -> list[DigestChunk]:
    if not deliveries:
        return []
    first = deliveries[0]
    scope = (first.user_id, first.channel, first.destination_version)
    if first.channel != "telegram" or any(
        (item.user_id, item.channel, item.destination_version) != scope for item in deliveries
    ):
        raise ValueError("Digest events must share a verified recipient and destination version")
    if len({item.id for item in deliveries}) != len(deliveries):
        raise ValueError("Digest contains duplicate deliveries")
    header, separator = "📋 Сводка объявлений:\n\n", "\n\n"
    chunks: list[DigestChunk] = []
    ids: list[str] = []
    text = header
    for delivery in deliveries:
        item = _format_item(delivery)
        if _units(header + item) > max_units:
            raise ValueError("Digest event cannot fit without losing its link")
        if ids and _units(text + separator + item) > max_units:
            chunks.append(DigestChunk(tuple(ids), text))
            ids, text = [], header
        text += (separator if ids else "") + item
        ids.append(delivery.id)
    if ids:
        chunks.append(DigestChunk(tuple(ids), text))
    return chunks
