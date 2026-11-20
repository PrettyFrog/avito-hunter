from hunter.transport.classification import (
    ResponseOutcome,
    classify_response,
)


def test_error_json_is_not_empty() -> None:
    cases = [
        '{"status":"error","totalCount":50,"catalog":{"items":[]}}',
        '{"status":"error","totalCount":0,"catalog":{"items":[]}}',
        '{"error":"rate limit","catalog":{"items":[]}}',
    ]
    for body in cases:
        res = classify_response(200, body)
        assert res.outcome != ResponseOutcome.EXPLICIT_EMPTY
        assert res.outcome == ResponseOutcome.UNKNOWN


def test_contradictory_count_and_empty_items_is_not_empty() -> None:
    cases = [
        '{"totalCount":50,"catalog":{"items":[]}}',
        '{"count":10,"items":[]}',
    ]
    for body in cases:
        res = classify_response(200, body)
        assert res.outcome != ResponseOutcome.EXPLICIT_EMPTY
        assert res.outcome == ResponseOutcome.UNKNOWN


def test_genuine_empty_json_is_explicit_empty() -> None:
    cases = [
        '{"status":"ok","totalCount":0,"catalog":{"items":[]}}',
        '{"totalCount":0,"catalog":{"items":[]}}',
        '{"catalog":{"items":[]}}',
    ]
    for body in cases:
        res = classify_response(200, body)
        assert res.outcome == ResponseOutcome.EXPLICIT_EMPTY


def test_anonymous_catalog_with_passport_footer_is_explicit_empty() -> None:
    cases = [
        '<div data-marker="catalog-empty">Nothing</div><a href="https://passport.avito.ru/">Login</a>',
        '<div data-marker="catalog-empty"></div><a href="/profile/login">Вход</a>',
    ]
    for html in cases:
        res = classify_response(200, html, require_auth=False)
        assert res.outcome == ResponseOutcome.EXPLICIT_EMPTY
        assert res.outcome != ResponseOutcome.AUTH_REQUIRED


def test_explicit_auth_gate_and_redirect_are_auth_required() -> None:
    res_redirect = classify_response(200, "<html>login</html>", url="https://passport.avito.ru/login")
    assert res_redirect.outcome == ResponseOutcome.AUTH_REQUIRED

    for gate_text in ("Войти или зарегистрироваться", "Войдите, чтобы продолжить"):
        res_gate = classify_response(200, gate_text, require_auth=False)
        assert res_gate.outcome == ResponseOutcome.AUTH_REQUIRED
