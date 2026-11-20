from __future__ import annotations

import io
from PIL import Image
import numpy as np
import pytest
from tests.fixtures.reference_storage import reference_storage as _reference_storage

from hunter.transport.qrator_codec import (
    decode_field,
    encode_field,
    generate_qrator_f,
    build_qrator_multipart,
    is_qrator_challenge,
)
from hunter.transport.geetest_solver import (
    find_puzzle_piece_position,
    solve_gobang,
    solve_geetest_pow,
    generate_w,
)


def test_qrator_codec_roundtrip():
    raw_f = "1920;1080;24;24;240;180;1;0;8;123456;789012"
    enc_f = encode_field(raw_f, "f")
    assert enc_f.startswith("5.")
    dec_f = decode_field(enc_f, "f")
    assert dec_f == raw_f

    raw_s = '{"monospace":false,"readOnly":false,"installedExtensions":[]}'
    enc_s = encode_field(raw_s, "s")
    assert not enc_s.startswith("5.")
    dec_s = decode_field(enc_s, "s")
    assert dec_s == raw_s


def test_qrator_generate_f_places_timestamp_and_hash():
    f_enc = generate_qrator_f(timestamp=1785160000, canvas_hash=33445566)
    f_raw = decode_field(f_enc, "f")
    parts = f_raw.split(";")
    assert len(parts) == 141
    assert parts[128] == "1785160000"
    assert parts[106] == "33445566"


def test_qrator_multipart_and_challenge_detection():
    ct, body = build_qrator_multipart("5.abc", "def", boundary="1234567812345678")
    assert b'name="f"\r\n\r\n5.abc' in body
    assert b'name="s"\r\n\r\ndef' in body
    assert 'boundary="1234567812345678"' in ct

    class FakeResponse:
        status_code = 302
        headers = {"server": "QRATOR"}
        text = '<html><meta http-equiv="refresh" content="1"><script src="/1234567890abcdef1234.js"></script></html>'

    assert is_qrator_challenge(FakeResponse())


def test_geetest_slide_solver_precision():
    bg = Image.new("RGB", (300, 160), color=(80, 80, 80))
    for x in range(120, 162):
        for y in range(40, 82):
            bg.putpixel((x, y), (240, 240, 240))
    sl = Image.new("RGB", (42, 42), color=(240, 240, 240))

    b_bg = io.BytesIO()
    bg.save(b_bg, format="PNG")
    b_sl = io.BytesIO()
    sl.save(b_sl, format="PNG")

    pos = find_puzzle_piece_position(b_sl.getvalue(), b_bg.getvalue())
    assert 90 <= pos <= 110


def test_geetest_gobang_solver():
    board = [
        [1, 1, 1, 0],
        [2, 2, 0, 0],
        [1, 0, 0, 0],
        [0, 0, 0, 0],
    ]
    res = solve_gobang(board)
    assert res is not None
    assert res[1] == [0, 3]  # fill position is (0, 3) to make 4-in-a-row!


def test_geetest_w_generation_and_encryption():
    data = {
        "lot_number": "lot_123456789012345678901234567890",
        "pow_detail": {"bits": 0, "hashfunc": "md5", "version": "basic", "datetime": "2026-11-02"},
    }
    w = generate_w(data, "2d9c743cf7d63dbc9db578a608196bcd", set_left=115.0)
    assert isinstance(w, str)
    assert len(w) > 256


@pytest.mark.asyncio
async def test_challenge_resolves_geetest_v4_automatically(reference_storage):
    from unittest.mock import AsyncMock, patch
    from hunter.domain.slots import SlotSnapshot, SlotRole, SlotHealth
    from hunter.transport.challenge import ChallengeHandoffService, ChallengeRequest, ChallengeStatus
    from hunter.transport.session_store import SessionStore

    repo, session_store = reference_storage
    await repo.register_slot("slot-1", "direct", health=SlotHealth.CHALLENGE)

    service = ChallengeHandoffService(slot_repo=repo, session_store=session_store)

    fake_cookies = [{"name": "cf_clearance", "value": "xyz", "domain": ".avito.ru", "path": "/"}]
    with patch("hunter.transport.geetest_solver.solve_geetest_v4_flow", AsyncMock(return_value=(True, fake_cookies))):
        req = ChallengeRequest(
            slot_id="slot-1",
            challenge_type="geetest",
            html="<html><div id='geetest_holder'></div></html>"
        )
        outcome = await service.handle_challenge(req)
        assert outcome.status == ChallengeStatus.SOLVED
        assert outcome.solved is True
        assert outcome.new_session_version == 2
        assert outcome.quarantined is False
        profile = await session_store.load_session("slot-1", expected_endpoint="direct")
        assert profile.cookies == fake_cookies
        assert profile.metadata.version == 2


def test_geetest_opencv_textured_precision():
    rng = np.random.default_rng(42)
    tex_bg = rng.integers(50, 200, (160, 300, 3), dtype=np.uint8)
    piece_crop = tex_bg[55:97, 145:187].copy()
    tex_bg[55:97, 145:187] = (tex_bg[55:97, 145:187] * 0.4).astype(np.uint8)

    b_bg = io.BytesIO()
    Image.fromarray(tex_bg).save(b_bg, format="PNG")
    b_sl = io.BytesIO()
    Image.fromarray(piece_crop).save(b_sl, format="PNG")

    pos = find_puzzle_piece_position(b_sl.getvalue(), b_bg.getvalue())
    assert pos == 125.0


@pytest.mark.asyncio
async def test_geetest_flow_retries_on_initial_verify_failure():
    from unittest.mock import AsyncMock, patch, MagicMock
    from hunter.transport.geetest_solver import solve_geetest_v4_flow
    import httpx

    fake_client = AsyncMock(spec=httpx.AsyncClient)
    load_resp = MagicMock(text='geetest_123({"data": {"lot_number": "lot_1", "captcha_type": "slide", "slice": "s.png", "bg": "bg.png"}})')
    ver_fail = MagicMock(text='geetest_123({"data": {"result": "fail"}})')
    ver_ok = MagicMock(text='geetest_123({"data": {"seccode": {"lot_number": "lot_1", "pass_token": "pt"}}})')
    fake_client.get = AsyncMock(side_effect=[load_resp, ver_fail, load_resp, ver_ok])

    with patch("hunter.transport.geetest_solver._get_firewall_captcha_id", AsyncMock(return_value="cid")), \
         patch("hunter.transport.geetest_solver._solve_geetest_w", AsyncMock(return_value="w_token")), \
         patch("hunter.transport.geetest_solver.submit_avito_captcha_verify", AsyncMock(return_value=True)), \
         patch("hunter.transport.recovery_transport.extract_scoped_cookies", MagicMock(return_value=[{"name": "v", "value": "1"}])):
        solved, cookies = await solve_geetest_v4_flow(client=fake_client)
        assert solved is True
        assert cookies == [{"name": "v", "value": "1"}]
        assert fake_client.get.call_count == 4


@pytest.mark.asyncio
async def test_geetest_flow_retries_on_timeout_exception():
    from unittest.mock import AsyncMock, patch, MagicMock
    from hunter.transport.geetest_solver import solve_geetest_v4_flow
    import httpx

    fake_client = AsyncMock(spec=httpx.AsyncClient)
    load_resp = MagicMock(text='geetest_123({"data": {"lot_number": "lot_1", "captcha_type": "slide", "slice": "s.png", "bg": "bg.png"}})')
    ver_ok = MagicMock(text='geetest_123({"data": {"seccode": {"lot_number": "lot_1", "pass_token": "pt"}}})')
    fake_client.get = AsyncMock(side_effect=[httpx.TimeoutException("Connection timed out"), load_resp, ver_ok])

    with patch("hunter.transport.geetest_solver._get_firewall_captcha_id", AsyncMock(return_value="cid")), \
         patch("hunter.transport.geetest_solver._solve_geetest_w", AsyncMock(return_value="w_token")), \
         patch("hunter.transport.geetest_solver.submit_avito_captcha_verify", AsyncMock(return_value=True)), \
         patch("hunter.transport.recovery_transport.extract_scoped_cookies", MagicMock(return_value=[{"name": "v", "value": "1"}])):
        solved, cookies = await solve_geetest_v4_flow(client=fake_client)
        assert solved is True
        assert cookies == [{"name": "v", "value": "1"}]
        assert fake_client.get.call_count == 3


@pytest.mark.asyncio
async def test_geetest_solve_delay_and_passtime():
    from unittest.mock import AsyncMock, patch, MagicMock
    from hunter.transport.geetest_solver import _solve_geetest_w, CAPTCHA_SOLVE_DELAY_SEC
    import httpx

    assert CAPTCHA_SOLVE_DELAY_SEC == 1.35

    fake_client = AsyncMock(spec=httpx.AsyncClient)
    fake_client.get = AsyncMock(return_value=MagicMock(content=b"dummy_bytes"))

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep, \
         patch("hunter.transport.geetest_solver.find_puzzle_piece_position", return_value=125.0):
        w = await _solve_geetest_w(fake_client, {"captcha_type": "slide", "slice": "s.png", "bg": "b.png"}, "cid")
        assert isinstance(w, str) and len(w) > 50
        mock_sleep.assert_called_once_with(1.35)


def test_unshared_upstreams_does_not_cross_block():
    from hunter.domain.request_budget import RequestBudget

    now = [100.0]
    budget = RequestBudget(enforce=True, spacing_sec=10.0, shared_upstreams=False, clock=lambda: now[0])
    first_avito = budget.before_attempt("ip1", "avito", deadline=200)
    assert first_avito.permit is not None
    budget.after_attempt(first_avito.permit)

    # With shared_upstreams=False, "other" is on an independent key and is not in cooldown from "avito"
    first_other = budget.before_attempt("ip1", "other", deadline=200)
    assert first_other.permit is not None
    assert first_other.reason == "admitted"

