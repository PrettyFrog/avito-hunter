import pytest
from hunter.transport.classification import ResponseOutcome, classify_response

FEED = '{"catalog":{"items":[{"id":123456789,"title":"item","urlPath":"/item_1","price":100}]}}'
EMPTY_JSON = '{"count":0,"catalog":{"items":[]}}'

@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 204, 206, 301, 302, 304])
def test_non_200_with_feed_is_not_ok_or_empty(status_code):
    res = classify_response(status_code, FEED)
    assert res.outcome not in (ResponseOutcome.OK, ResponseOutcome.EXPLICIT_EMPTY)

@pytest.mark.parametrize("status_code", [400, 404, 500, 503])
def test_non_200_with_zero_count_is_not_explicit_empty(status_code):
    res = classify_response(status_code, EMPTY_JSON)
    assert res.outcome != ResponseOutcome.EXPLICIT_EMPTY
    assert res.outcome != ResponseOutcome.OK

def test_http_200_feed_is_ok():
    res = classify_response(200, FEED)
    assert res.outcome == ResponseOutcome.OK
    assert len(res.hits) == 1

def test_http_200_empty_is_explicit_empty():
    res = classify_response(200, EMPTY_JSON)
    assert res.outcome == ResponseOutcome.EXPLICIT_EMPTY
