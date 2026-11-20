"""HTTP transport for VK community Bots Long Poll."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class VkLongPollError(RuntimeError):
    """Sanitized Long Poll failure that never includes credentials or response text."""


@dataclass(frozen=True, slots=True)
class VkPollBatch:
    updates: tuple[dict[str, Any], ...] = ()
    next_ts: str | None = None


@dataclass(slots=True)
class _VkSession:
    server: str
    key: str
    ts: str


class VkLongPollClient:
    """Fetch community update batches and advance timestamps only after caller ack."""

    API_URL = "https://api.vk.com/method/groups.getLongPollServer"

    def __init__(
        self,
        group_id: int,
        access_token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_version: str = "5.199",
    ) -> None:
        if group_id <= 0 or not access_token:
            raise ValueError("VK group ID and access token are required")
        self.group_id = group_id
        self._access_token = access_token
        self._api_version = api_version
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._session: _VkSession | None = None

    async def _load_session(self) -> None:
        response = await self._client.post(
            self.API_URL,
            data={
                "group_id": self.group_id,
                "access_token": self._access_token,
                "v": self._api_version,
            },
        )
        payload = self._json(response)
        session = payload.get("response")
        if not isinstance(session, dict):
            raise VkLongPollError("VK did not return a community Long Poll session")
        server = session.get("server")
        key = session.get("key")
        ts = session.get("ts")
        if not isinstance(server, str) or not isinstance(key, str) or ts is None:
            raise VkLongPollError("VK returned an incomplete Long Poll session")
        _validate_server(server)
        self._session = _VkSession(server, key, str(ts))

    async def poll(self) -> VkPollBatch:
        if self._session is None:
            await self._load_session()
        session = self._session
        if session is None:
            raise VkLongPollError("VK Long Poll session is unavailable")
        response = await self._client.get(
            session.server,
            params={
                "act": "a_check",
                "key": session.key,
                "ts": session.ts,
                "wait": 25,
                "version": 3,
            },
        )
        payload = self._json(response)
        failure = payload.get("failed")
        if failure == 1:
            return VkPollBatch(next_ts=str(payload.get("ts", session.ts)))
        if failure in (2, 3):
            self._session = None
            return VkPollBatch()
        updates = payload.get("updates", [])
        if not isinstance(updates, list):
            raise VkLongPollError("VK returned malformed Long Poll updates")
        return VkPollBatch(
            tuple(item for item in updates if isinstance(item, dict)),
            str(payload.get("ts", session.ts)),
        )

    def ack(self, batch: VkPollBatch) -> None:
        if self._session is not None and batch.next_ts is not None:
            self._session.ts = batch.next_ts

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except Exception:
            raise VkLongPollError("VK Long Poll request failed") from None
        if not isinstance(payload, dict) or "error" in payload:
            raise VkLongPollError("VK Long Poll returned an API error")
        return payload


def _validate_server(server: str) -> None:
    parsed = urlsplit(server)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        raise VkLongPollError("VK returned an invalid Long Poll server") from None
    if parsed.scheme != "https" or not (host == "vk.com" or host.endswith(".vk.com")):
        raise VkLongPollError("VK returned an untrusted Long Poll server")
    if parsed.username or parsed.password or port not in (None, 443):
        raise VkLongPollError("VK returned an invalid Long Poll server")
