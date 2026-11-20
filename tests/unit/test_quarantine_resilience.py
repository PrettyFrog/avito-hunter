"""TDD tests for quarantine resilience and speed optimization."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from hunter.domain.slots import SlotSignal, SlotSnapshot, SlotHealth, SlotRole
from hunter.application.poll_attempts import can_fallback_to_html, execute_retry_with_peer_failover
from hunter.application.public_poll_runtime import _assignment_poll_state, QueryAssignment
from hunter.transport.challenge import ChallengeHandoffService


def test_can_fallback_to_html_contract():
    canon = "https://www.avito.ru/moskva/tovary"
    items_url = "https://www.avito.ru/web/1/js/items?categoryId=101"
    
    # 404/410 obsolete routes fallback to HTML
    resp_404 = SimpleNamespace(status_code=404, text="not found", headers={}, error=None)
    assert can_fallback_to_html(items_url, canon, resp_404) is True

    # 403 access failures do NOT blind-fallback to HTML before challenge handler
    resp_403 = SimpleNamespace(status_code=403, text='{"error":"denied"}', headers={}, error=None)
    assert can_fallback_to_html(items_url, canon, resp_403) is False

    # Target is already canonical -> never fallback
    assert can_fallback_to_html(canon, canon, resp_404) is False


@pytest.mark.asyncio
async def test_peer_failover_recovers_200_on_retry():
    runtime = SimpleNamespace(
        _retry_fetch=AsyncMock(),
        _handle_challenge=AsyncMock(),
        challenge_service=object(),
        metrics=MagicMock(),
        _report_slot_failure=AsyncMock(),
    )
    assignment = SimpleNamespace(slot_id="slot-test", canonical_url="https://www.avito.ru/search")
    fetcher = SimpleNamespace(curl_client=object(), curl_client_b=object())

    # Primary retry fails with 403
    prim_resp = SimpleNamespace(status_code=403, headers={}, text="err", error=None, elapsed_sec=0.1)
    prim_cls = SimpleNamespace(suggested_signal=SlotSignal.CHALLENGE_DETECTED)

    # Peer retry succeeds with 200 OK
    peer_resp = SimpleNamespace(status_code=200, headers={}, text="ok", error=None, elapsed_sec=0.1)
    peer_cls = SimpleNamespace(suggested_signal=SlotSignal.PROBE_OK)

    runtime._retry_fetch.side_effect = [
        (prim_resp, prim_cls),     # Primary retry
        (peer_resp, peer_cls),     # Peer retry
    ]

    res = await execute_retry_with_peer_failover(
        runtime, assignment, fetcher, "https://www.avito.ru/items", None, "user_b",
    )

    assert res is not None
    resp, cls = res
    assert resp.status_code == 200
    # Verify slot was NOT reported as failed/quarantined
    runtime._report_slot_failure.assert_not_called()


@pytest.mark.asyncio
async def test_paused_subscription_returns_paused_state():
    mock_cursor = AsyncMock()
    # 1. Assignment is active and slot is READY
    # 2. But subscriptions table has NO rows with enabled = 1
    mock_cursor.fetchone.side_effect = [
        (1, 1, "ready"),  # assignments row: gen=1, active=1, health=ready
        None,             # subscriptions row: no enabled subscriptions
    ]
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_cursor
    conn_mgr = SimpleNamespace(
        open_read_connection=AsyncMock(return_value=mock_conn),
        close_reader=AsyncMock(),
    )
    asg = QueryAssignment("slot-home", "direct", "q_iphone", "https://avito.ru", 1, 0)
    state = await _assignment_poll_state(conn_mgr, asg)
    assert state == "paused"


@pytest.mark.asyncio
async def test_unpaused_subscription_returns_current_state():
    mock_cursor = AsyncMock()
    # 1. Assignment is active and slot is READY
    # 2. Subscriptions table HAS a row with enabled = 1
    mock_cursor.fetchone.side_effect = [
        (1, 1, "ready"),  # assignments row: gen=1, active=1, health=ready
        (1,),             # subscriptions row: enabled=1 exists
    ]
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_cursor
    conn_mgr = SimpleNamespace(
        open_read_connection=AsyncMock(return_value=mock_conn),
        close_reader=AsyncMock(),
    )
    asg = QueryAssignment("slot-home", "direct", "q_iphone", "https://avito.ru", 1, 0)
    state = await _assignment_poll_state(conn_mgr, asg)
    assert state == "current"


@pytest.mark.asyncio
async def test_deleted_subscription_returns_stale_state():
    mock_cursor = AsyncMock()
    # Assignment active = 0 (deleted)
    mock_cursor.fetchone.return_value = (1, 0, "ready")
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_cursor
    conn_mgr = SimpleNamespace(
        open_read_connection=AsyncMock(return_value=mock_conn),
        close_reader=AsyncMock(),
    )
    asg = QueryAssignment("slot-home", "direct", "q_iphone", "https://avito.ru", 1, 0)
    state = await _assignment_poll_state(conn_mgr, asg)
    assert state == "stale"


@pytest.mark.asyncio
async def test_save_challenge_state_merges_cookies():
    session_store = SimpleNamespace(
        load_session=AsyncMock(return_value=SimpleNamespace(
            cookies=[{"name": "u", "value": "user_id_123", "domain": ".avito.ru"}]
        )),
    )
    slot_repo = SimpleNamespace()
    service = ChallengeHandoffService(slot_repo=slot_repo, session_store=session_store)

    slot = SlotSnapshot(
        slot_id="slot-home", endpoint="direct", health=SlotHealth.READY, role=SlotRole.ACTIVE,
    )
    new_cookies = [{"name": "_avisc", "value": "firewall_token", "domain": ".avito.ru"}]

    with patch("hunter.storage.recovery_commit.guarded_recovery_commit", AsyncMock(return_value=2)) as mock_commit:
        ver = await service._save_challenge_state(slot, page=None, cookies=new_cookies, client_identity="user_b")
        assert ver == 2
        mock_commit.assert_called_once()
        passed_cookies = mock_commit.call_args[0][3]
        cookie_names = {c["name"] for c in passed_cookies}
        # Verify both the old session cookie and new firewall cookie are preserved
        assert "u" in cookie_names
        assert "_avisc" in cookie_names


@pytest.mark.asyncio
async def test_retry_forces_canonical_html():
    runtime = SimpleNamespace(
        _retry_fetch=AsyncMock(),
        _handle_challenge=AsyncMock(),
        challenge_service=object(),
        metrics=MagicMock(),
        _report_slot_failure=AsyncMock(),
    )
    assignment = SimpleNamespace(slot_id="slot-test", canonical_url="https://www.avito.ru/search")
    fetcher = SimpleNamespace(curl_client=object(), curl_client_b=object())

    # Primary retry succeeds on canonical HTML
    ok_resp = SimpleNamespace(status_code=200, headers={}, text="ok", error=None, elapsed_sec=0.1)
    ok_cls = SimpleNamespace(suggested_signal=SlotSignal.PROBE_OK)
    runtime._retry_fetch.return_value = (ok_resp, ok_cls)

    with patch("hunter.transport.catalog_api.invalidate_poll_target") as mock_inval:
        res = await execute_retry_with_peer_failover(
            runtime, assignment, fetcher, "https://www.avito.ru/web/1/js/items", {"x-test": "1"}, "user_b",
        )
        assert res is not None
        mock_inval.assert_called_once_with("https://www.avito.ru/search")
        # Ensure _retry_fetch was called with canonical HTML and hdrs=None
        call_kwargs = runtime._retry_fetch.call_args[1]
        assert call_kwargs["target_url"] == "https://www.avito.ru/search"
        assert call_kwargs["hdrs"] is None


@pytest.mark.asyncio
async def test_peer_failover_solves_secondary_challenge_instead_of_quarantine():
    runtime = SimpleNamespace(
        _retry_fetch=AsyncMock(),
        _handle_challenge=AsyncMock(),
        challenge_service=object(),
        metrics=MagicMock(),
        _report_slot_failure=AsyncMock(),
    )
    assignment = SimpleNamespace(slot_id="slot-test", canonical_url="https://www.avito.ru/search")
    fetcher = SimpleNamespace(curl_client=object(), curl_client_b=object())

    # 1. Primary retry on user_a fails with 439 Challenge
    prim_resp = SimpleNamespace(status_code=439, headers={}, text="pow", error=None, elapsed_sec=0.1)
    prim_cls = SimpleNamespace(suggested_signal=SlotSignal.CHALLENGE_DETECTED)

    # 2. Peer retry on user_b also receives 439 Challenge
    peer_chal_resp = SimpleNamespace(status_code=439, headers={}, text="pow-b", error=None, elapsed_sec=0.1)
    peer_chal_cls = SimpleNamespace(suggested_signal=SlotSignal.CHALLENGE_DETECTED)

    # 3. Solver solves challenge on user_b
    runtime._handle_challenge.return_value = SimpleNamespace(solved=True, status="SOLVED")

    # 4. Peer retry after solver succeeds with 200 OK
    peer_ok_resp = SimpleNamespace(status_code=200, headers={}, text="ok", error=None, elapsed_sec=0.1)
    peer_ok_cls = SimpleNamespace(suggested_signal=SlotSignal.PROBE_OK)

    runtime._retry_fetch.side_effect = [
        (prim_resp, prim_cls),           # Primary retry (user_a)
        (peer_chal_resp, peer_chal_cls), # Peer retry (user_b) -> 439
        (peer_ok_resp, peer_ok_cls),     # Peer retry after solve -> 200 OK
    ]

    with patch("asyncio.sleep", new=AsyncMock()):
        res = await execute_retry_with_peer_failover(
            runtime, assignment, fetcher, "https://www.avito.ru/search", None, "user_a",
        )

    assert res is not None
    assert res[0].status_code == 200
    runtime._handle_challenge.assert_called_once_with(
        assignment, peer_chal_resp, allow_quarantine=True, client_identity="user_b",
    )
    runtime._report_slot_failure.assert_not_called()

