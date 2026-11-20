"""Integration smoke test for Camoufox session warmup and curl_cffi HTTP fetch."""
import time
import pytest
from hunter.domain.slots import SlotHealth, SlotRole, SlotSnapshot
from hunter.transport.browser_factory import create_slot_browser_factory
from hunter.transport.http_fetch import HttpSlotFetcher, SlotFetchConfig
from hunter.transport.session_store import SessionStore


@pytest.mark.allow_network
@pytest.mark.asyncio
async def test_camoufox_warmup_and_http_fetch(tmp_path):
    store = SessionStore(base_dir=tmp_path / "sessions")
    slot = SlotSnapshot(
        slot_id="slot-smoke",
        endpoint="direct",
        health=SlotHealth.READY,
        role=SlotRole.ACTIVE,
        generation=1,
    )

    class DummySettings:
        avito_base_url = "https://www.avito.ru"

    print("\n--- [Step 1: Camoufox Warmup] ---")
    start_warmup = time.perf_counter()
    factory = create_slot_browser_factory(DummySettings(), session_store=store)
    page = await factory(slot)
    warmup_duration = time.perf_counter() - start_warmup

    session_payload = await store.load_session("slot-smoke", expected_endpoint="direct")
    assert session_payload is not None, "Warmed session payload must be persisted"

    cookie_names = [c.get("name") for c in session_payload.cookies]
    ua = session_payload.storage_state.get("user_agent", "")
    print(f"Warmup completed in {warmup_duration:.2f}s")
    print(f"Extracted User-Agent: {ua}")
    print(f"Cookies acquired ({len(cookie_names)}): {cookie_names}")
    await page.close()

    assert "Firefox" in ua, f"User-Agent must be Firefox, got: {ua}"
    assert len(cookie_names) > 0, "Expected at least 1 cookie from warmup"

    print("\n--- [Step 2: 3 Sequential Fetches via HttpSlotFetcher] ---")
    fetcher = HttpSlotFetcher(
        slot_id="slot-smoke",
        endpoint="direct",
        config=SlotFetchConfig(timeout_sec=15.0),
        session_store=store,
    )
    loaded_cookies_count = await fetcher.load_cookies_from_store()
    print(f"Loaded {loaded_cookies_count} cookies into HttpSlotFetcher")

    target_url = "https://www.avito.ru/rossiya"
    for i in range(1, 4):
        resp = await fetcher.fetch(target_url)
        print(
            f"Request #{i}: Status={resp.status_code}, WireBytes={resp.wire_body_bytes}, "
            f"Elapsed={resp.elapsed_sec:.2f}s, Blocked={resp.is_blocked}, RateLimited={resp.is_rate_limited}"
        )
        assert resp.error is None, f"Fetch error: {resp.error}"
        assert resp.status_code == 200, f"Expected 200 OK, got {resp.status_code}"
        assert not resp.is_blocked, "Must not be blocked by anti-bot"
        assert not resp.is_rate_limited, "Must not be rate limited"

    await fetcher.close()
    print("--- [Smoke Test Complete: All 3 requests returned 200 OK] ---\n")
