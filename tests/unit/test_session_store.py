"""Unit tests for versioned cookie SessionStore (S5 / T-478).

Covers:
- Atomic replacement and resilience to crash before replace.
- Rejection of stale version or stale generation.
- Rejection of IP identity / endpoint mismatch.
- Isolation of cookies between slots.
- Restrictive file permissions (0o600 file, 0o700 directory).
- Secrets off logs / redacted representations.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from hunter.transport.session_store import (
    EndpointMismatchError,
    SessionPayload,
    SessionStore,
    StaleSessionVersionError,
)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


@pytest.mark.asyncio
async def test_session_save_load_and_atomic_replace(store: SessionStore):
    cookies = [{"name": "sessid", "value": "secret_abc", "domain": ".avito.ru", "path": "/"}]
    meta = await store.save_session(
        slot_id="slot_01",
        endpoint="http://10.0.0.1:8080",
        version=1,
        generation=1,
        cookies=cookies,
    )
    assert meta.slot_id == "slot_01"
    assert meta.version == 1
    assert meta.generation == 1
    assert meta.cookie_hash != ""

    payload = await store.load_session("slot_01", expected_endpoint="http://10.0.0.1:8080")
    assert payload is not None
    assert payload.metadata.version == 1
    assert len(payload.cookies) == 1
    assert payload.cookies[0]["value"] == "secret_abc"


@pytest.mark.asyncio
async def test_crash_before_replace_preserves_original(store: SessionStore):
    # 1. Save original valid session
    orig_cookies = [{"name": "auth", "value": "original_token", "domain": ".avito.ru"}]
    await store.save_session(
        slot_id="slot_crash",
        endpoint="http://10.0.0.1:8080",
        version=1,
        generation=1,
        cookies=orig_cookies,
    )

    # 2. Simulate crash / failure during atomic replace
    with patch("os.replace", side_effect=OSError("Disk full or process killed before replace")):
        with pytest.raises(OSError, match="before replace"):
            await store.save_session(
                slot_id="slot_crash",
                endpoint="http://10.0.0.1:8080",
                version=2,
                generation=1,
                cookies=[{"name": "auth", "value": "new_token"}],
            )

    # 3. Target file must remain intact and contain original data
    recovered = await store.load_session("slot_crash")
    assert recovered is not None
    assert recovered.metadata.version == 1
    assert recovered.cookies[0]["value"] == "original_token"


@pytest.mark.asyncio
async def test_stale_generation_and_version_rejected(store: SessionStore):
    await store.save_session(
        slot_id="slot_ver",
        endpoint="http://10.0.0.1:8080",
        version=2,
        generation=3,
        cookies=[{"name": "a", "value": "1"}],
    )

    # Rejection of smaller version
    with pytest.raises(StaleSessionVersionError, match="Stale session version"):
        await store.save_session(
            slot_id="slot_ver",
            endpoint="http://10.0.0.1:8080",
            version=1,
            generation=4,
            cookies=[{"name": "a", "value": "2"}],
        )

    # Rejection of same version with stale/equal generation
    with pytest.raises(StaleSessionVersionError, match="Stale generation"):
        await store.save_session(
            slot_id="slot_ver",
            endpoint="http://10.0.0.1:8080",
            version=2,
            generation=3,
            cookies=[{"name": "a", "value": "2"}],
        )

    # Advancing generation succeeds
    meta = await store.save_session(
        slot_id="slot_ver",
        endpoint="http://10.0.0.1:8080",
        version=2,
        generation=4,
        cookies=[{"name": "a", "value": "3"}],
    )
    assert meta.generation == 4


@pytest.mark.asyncio
async def test_endpoint_identity_mismatch_rejected(store: SessionStore):
    await store.save_session(
        slot_id="slot_proxy",
        endpoint="http://192.168.1.50:8080",
        version=1,
        generation=1,
        cookies=[{"name": "sess", "value": "proxy_session"}],
    )

    # Loading with correct endpoint succeeds
    assert (
        await store.load_session("slot_proxy", expected_endpoint="http://192.168.1.50:8080")
        is not None
    )

    # Loading with mismatched endpoint raises EndpointMismatchError
    with pytest.raises(EndpointMismatchError, match="Endpoint mismatch"):
        await store.load_session("slot_proxy", expected_endpoint="http://10.0.0.99:8080")


@pytest.mark.asyncio
async def test_cookies_isolated_between_slots(store: SessionStore):
    await store.save_session(
        slot_id="slot_alpha",
        endpoint="http://10.0.0.1:8080",
        version=1,
        generation=1,
        cookies=[{"name": "tok", "value": "alpha_secret"}],
    )
    await store.save_session(
        slot_id="slot_beta",
        endpoint="http://10.0.0.2:8080",
        version=1,
        generation=1,
        cookies=[{"name": "tok", "value": "beta_secret"}],
    )

    alpha = await store.load_session("slot_alpha")
    beta = await store.load_session("slot_beta")

    assert alpha is not None and beta is not None
    assert alpha.cookies[0]["value"] == "alpha_secret"
    assert beta.cookies[0]["value"] == "beta_secret"
    assert store._slot_path("slot_alpha") != store._slot_path("slot_beta")


@pytest.mark.asyncio
async def test_restrictive_file_permissions(store: SessionStore):
    await store.save_session(
        slot_id="slot_secure",
        endpoint="http://10.0.0.1:8080",
        version=1,
        generation=1,
        cookies=[{"name": "c", "value": "v"}],
    )

    file_path = store._slot_path("slot_secure")
    file_mode = stat.S_IMODE(os.stat(file_path).st_mode)
    dir_mode = stat.S_IMODE(os.stat(store.base_dir).st_mode)

    # File permissions must be 0o600 (owner read/write only)
    assert file_mode == 0o600
    # Directory permissions must be 0o700 (owner rwx only)
    assert dir_mode == 0o700


def test_secrets_off_logs_and_redaction():
    secret_value = "SUPER_SECRET_QRATOR_COOKIE_VALUE_12345"
    from hunter.transport.session_store import SessionMetadata

    meta = SessionMetadata(
        slot_id="slot_sec",
        endpoint="http://10.0.0.1",
        version=1,
        generation=1,
        cookie_hash="dummy_hash",
        updated_at="2026-10-23T12:00:00Z",
    )
    payload = SessionPayload(
        metadata=meta,
        cookies=[{"name": "qrator_jsid", "value": secret_value, "domain": ".avito.ru"}],
    )
    repr_str = repr(payload)
    str_str = str(payload)

    # Ensure secret cookie value is completely absent from repr and str
    assert secret_value not in repr_str
    assert secret_value not in str_str
    assert "qrator_jsid" in repr_str

    # Redacted dict also redacts secret
    redacted = payload.redacted_dict()
    assert redacted["cookies"][0]["value"] == "[REDACTED]"
