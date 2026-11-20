"""Validate a positive, integral ruble price without silently changing it."""
import re


def parse_public_price(text: str) -> int:
    raw = text.strip()
    raw = re.sub(r"\s*(?:₽|руб(?:лей|ля|\.)?|р\.?)\s*$", "", raw, flags=re.IGNORECASE)
    if not re.fullmatch(r"[0-9]+(?:[ \u00a0][0-9]+)*", raw):
        raise ValueError("Укажите целое положительное число рублей")
    digits = raw.replace(" ", "").replace("\u00a0", "").lstrip("0")
    if not digits or len(digits) > 19:
        raise ValueError("Укажите целое положительное число рублей")
    price = int(digits)
    if price > 9223372036854775807:
        raise ValueError("Слишком большая цена")
    return price
