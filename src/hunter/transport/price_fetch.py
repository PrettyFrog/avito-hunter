"""Transport adapter for card price checks (S7 / T-508)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import time
import httpx

from hunter.collect.parsers.card import extract_card_price
from hunter.transport.metered_http import HttpDeferred


@dataclass(frozen=True, slots=True)
class PriceFetchResult:
    status: Literal["ok", "closed", "unknown", "deferred"]
    price: int | None = None
    error: str = ""
    retry_after_sec: float | None = None


class PriceFetchAdapter:
    """Card price fetching adapter with 404/closed classification."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_body_bytes: int = 500 * 1024,
        clock=time.monotonic,
    ) -> None:
        self.client = client
        self.max_body_bytes = max_body_bytes
        self.clock = clock

    async def fetch_card_price(self, url: str) -> PriceFetchResult:
        if self.client is None:
            return PriceFetchResult(status="unknown", error="no_client")
        try:
            resp = await self.client.get(url, timeout=10.0)
            if resp.status_code == 404:
                return PriceFetchResult(status="closed", error="HTTP 404")
            if resp.status_code != 200:
                return PriceFetchResult(status="unknown", error=f"HTTP {resp.status_code}")
            if len(resp.content) > self.max_body_bytes:
                return PriceFetchResult(status="unknown", error="response_too_large")
            price, st = extract_card_price(resp.text, url=url)
            return PriceFetchResult(status=st, price=price)
        except HttpDeferred as deferred:
            return PriceFetchResult(status="deferred", error="request_deferred",
                                    retry_after_sec=max(0.0, deferred.not_before - self.clock()))
        except Exception as exc:
            return PriceFetchResult(status="unknown", error=str(exc))

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def make_price_client(gateway=None, client: httpx.AsyncClient | None = None) -> httpx.AsyncClient:
    if client is None:
        if gateway is None:
            client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        else:
            from hunter.transport.attempt_labels import AttemptLabels
            from hunter.transport.http_attempts import observed_httpx_client
            client = observed_httpx_client(labels=AttemptLabels("price-watch", "unknown", "default"),
                                           timeout=10.0, follow_redirects=True)
    if gateway is not None:
        from hunter.transport.metered_http import meter_httpx_client
        meter_httpx_client(client, gateway, egress_group="default", generation=0, timeout_sec=10)
    return client
