"""Price edits preserve Avito filters and use the exact new ceiling."""
import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest

from hunter.application.subscription_rebind import change_query_price
from hunter.domain.query_url import validate_and_canonicalize_query_url


def test_rewrite_pmax_and_encoded_f_price_without_losing_filters():
    encoded = base64.b64encode(json.dumps({"to": 100000, "brand": "Apple"}).encode()).decode()
    original = validate_and_canonicalize_query_url(
        f"https://avito.ru/moskva?q=mac&f={encoded}&pmax=100000", 100000
    )
    result = change_query_price(original, 120000)
    params = parse_qs(urlparse(result).query)
    assert params["pmax"] == ["120000"] and params["q"] == ["mac"]
    decoded = json.loads(base64.b64decode(params["f"][0] + "=="))
    assert decoded == {"to": 120000, "brand": "Apple"}


@pytest.mark.parametrize("price", [0, -1, 2**63])
def test_rewrite_rejects_invalid_price(price):
    with pytest.raises(ValueError):
        change_query_price("https://avito.ru/moskva?q=mac&pmax=100000", price)
