"""Helpers for changing the physical query behind a public subscription."""
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from hunter.domain.query_url import sanitize_f_param, validate_and_canonicalize_query_url


def change_query_price(canonical_url: str, price: int) -> str:
    """Replace explicit and decodable encoded ceilings without dropping filters."""
    if type(price) is not int or not 0 < price <= 9223372036854775807:
        raise ValueError("Цена должна быть положительным целым числом рублей")
    parsed = urlparse(canonical_url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    params.pop("pmax", None)
    if params.get("f"):
        params["f"] = [sanitize_f_param(value, price) for value in params["f"]]
    candidate = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
    return validate_and_canonicalize_query_url(candidate, price)
