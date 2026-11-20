from __future__ import annotations

import hashlib
import json
import base64
from unittest.mock import AsyncMock, MagicMock
import pytest

from hunter.collect.firewall_pow import (
    find_nonce,
    extract_pow_challenge,
    solve_firewall_pow,
)


def test_find_nonce_computes_valid_sha256_hash():
    challenge_id = "test_challenge_id_12345"
    compl = 4
    nonce = find_nonce(challenge_id, complexity=compl)
    assert nonce is not None

    h = hashlib.sha256(f"{challenge_id}:{nonce}".encode("ascii")).hexdigest()
    assert h.startswith("0000")


def test_find_nonce_with_different_complexity():
    challenge_id = "avito_pow_abc"
    compl = 3
    nonce = find_nonce(challenge_id, complexity=compl)
    assert nonce is not None
    h = hashlib.sha256(f"{challenge_id}:{nonce}".encode("ascii")).hexdigest()
    assert h.startswith("000")


def test_extract_pow_challenge_from_html():
    challenge_token = "eyJhbGciOiJIUzI1NiJ9.eyJpZCI6ImF2aXRvLTEyMyIsImNvbXBsIjo0fQ.test_sig"
    html_sample = f"""
    <html>
      <head><title>Доступ ограничен</title></head>
      <body>
        <script>
          window.pow_challenge = "{challenge_token}";
        </script>
      </body>
    </html>
    """
    challenge = extract_pow_challenge(html_sample)
    assert challenge == challenge_token


def test_extract_pow_challenge_from_json_string():
    html_json = '{"challenge": "challenge_token_999", "status": 439}'
    challenge = extract_pow_challenge(html_json)
    assert challenge == "challenge_token_999"


@pytest.mark.asyncio
async def test_solve_firewall_pow_flow_success():
    # Mock Playwright page
    page = MagicMock()

    payload_dict = {"id": "pow_target_id", "compl": 4}
    jwt_payload = base64.b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    fake_jwt = f"header.{jwt_payload}.signature"

    # 1. page.evaluate returning step 1 (get challenge)
    # 2. page.evaluate returning step 2 (verify)
    async def fake_evaluate(script, *args, **kwargs):
        if "firewallPow/get" in script:
            return {
                "success": True,
                "id": "pow_target_id",
                "compl": 4,
                "jwt": fake_jwt,
            }
        elif "firewallPow/verify" in script:
            return {
                "verified": True,
                "data": {"success": {"result": {"verified": True, "unblock_ttl": 1800}}},
            }
        return {"success": False}

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    unblocked = await solve_firewall_pow(page)
    assert unblocked is True
