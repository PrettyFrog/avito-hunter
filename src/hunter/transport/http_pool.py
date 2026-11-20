"""Connection pool maintaining active, isolated HttpSlotFetcher or InTabSlotFetcher instances across slots."""

from __future__ import annotations

from contextlib import suppress
from typing import Any
import httpx

from hunter.transport.proxy_credentials import ProxyCredentialStore
from hunter.transport.session_store import SessionStore


class SlotHttpPool:
    """Maintains active, isolated HttpSlotFetcher or InTabSlotFetcher instances across slots."""

    def __init__(
        self, session_store: SessionStore | None = None, config: Any | None = None,
        credential_store: ProxyCredentialStore | None = None, browser_factory: Any | None = None,
        use_browser_fetch: bool = False, pow_executor: Any = None,
        gateway: Any = None, egress_groups: Any = None, browser_fetch_direct: bool = False,
        browser_fetch_slots: str = "",
    ) -> None:
        from hunter.transport.http_fetch import SlotFetchConfig
        self.session_store, self.config = session_store, config or SlotFetchConfig()
        self.credential_store, self.browser_factory = credential_store, browser_factory
        self.use_browser_fetch, self._fetchers, self.pow_executor = use_browser_fetch, {}, pow_executor
        self.browser_fetch_direct = browser_fetch_direct
        self.browser_fetch_slots = {s.strip() for s in str(browser_fetch_slots).split(",") if s.strip()}
        self.gateway, self.egress_groups = gateway, egress_groups

    def _use_in_tab(self, slot_id: str, endpoint: str) -> bool:
        if not (self.browser_factory and self.use_browser_fetch): return False
        if self.browser_fetch_slots: return slot_id in self.browser_fetch_slots
        return True

    def get_fetcher(
        self, slot_id: str, endpoint: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
    ) -> Any:
        from hunter.transport.http_fetch import HttpFetchError, HttpSlotFetcher
        if not endpoint: raise HttpFetchError("Slot proxy endpoint is required")
        existing = self._fetchers.get(slot_id)
        if existing is not None and getattr(existing, "endpoint", None) != endpoint:
            raise HttpFetchError("Slot proxy endpoint changed while fetcher is active")
        if slot_id not in self._fetchers:
            if self._use_in_tab(slot_id, endpoint):
                from hunter.transport.in_tab_fetch import InTabSlotFetcher
                self._fetchers[slot_id] = InTabSlotFetcher(
                    slot_id=slot_id, endpoint=endpoint, browser_factory=self.browser_factory, timeout_sec=self.config.timeout_sec, session_store=self.session_store,
                )
            else:
                creds = self.credential_store.load(slot_id, endpoint) if self.credential_store and endpoint not in {"", "direct", "direct://"} else None
                self._fetchers[slot_id] = HttpSlotFetcher(
                    slot_id=slot_id, endpoint=endpoint, config=self.config, session_store=self.session_store, transport=transport, proxy_credentials=creds, pow_executor=self.pow_executor,
                    gateway=self.gateway, egress_group=self.egress_groups.group_for(slot_id, endpoint) if self.egress_groups else "default",
                )
        return self._fetchers[slot_id]

    async def retire_fetcher(self, slot_id: str) -> None:
        """Controlled retirement of a pinned fetcher when slot endpoint changes."""
        fetcher = self._fetchers.pop(slot_id, None)
        if fetcher is not None and hasattr(fetcher, "close"):
            with suppress(Exception): await fetcher.close()

    async def close_all(self) -> None:
        for fetcher in list(self._fetchers.values()): await fetcher.close()
        self._fetchers.clear()
