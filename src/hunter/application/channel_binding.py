"""Channel destination binding and ownership verification (S6 / T-510)."""

from __future__ import annotations

import hmac
import hashlib
import secrets
import time
import aiosqlite


class BindingError(Exception):
    pass


class ForgedTokenError(BindingError):
    pass


class ExpiredTokenError(BindingError):
    pass


class TokenReplayError(BindingError):
    pass


class CrossUserBindingError(BindingError):
    pass


class ChannelBindingService:
    """Manages channel destination tokens, verified binding, and tenant-scoped revocation."""

    def __init__(self, secret_key: str) -> None:
        if len(secret_key.encode("utf-8")) < 32:
            raise ValueError("Channel binding secret must be at least 32 bytes")
        self._secret = secret_key

    @classmethod
    def from_settings(cls, settings: object) -> ChannelBindingService:
        secret = getattr(settings, "channel_binding_secret", None)
        if secret is None:
            raise ValueError("Channel binding secret is not configured")
        return cls(secret.get_secret_value())

    def generate_token(self, user_id: int, channel: str = "vk", ttl_seconds: int = 600) -> str:
        now = int(time.time())
        salt = secrets.token_hex(16)
        payload = f"{user_id}:{channel}:{now}:{salt}"
        sig = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"bnd_{user_id}_{now}_{salt}_{sig}"

    def verify_token(
        self,
        token: str,
        expected_user_id: int,
        expected_channel: str = "vk",
        ttl_seconds: int = 600,
    ) -> None:
        parts = token.split("_")
        if len(parts) != 5 or parts[0] != "bnd":
            raise ForgedTokenError("Malformed or forged binding token")
        try:
            token_uid, ts, salt, sig = int(parts[1]), int(parts[2]), parts[3], parts[4]
        except (ValueError, IndexError) as exc:
            raise ForgedTokenError("Malformed binding token parameters") from exc
        if token_uid != expected_user_id:
            raise CrossUserBindingError(
                f"Cross-user binding denied: token for {token_uid}, actor {expected_user_id}"
            )
        now = int(time.time())
        if now - ts > ttl_seconds or ts > now + 30:
            raise ExpiredTokenError("Binding token has expired")
        expected_payload = f"{token_uid}:{expected_channel}:{ts}:{salt}"
        expected_sig = hmac.new(
            self._secret.encode(), expected_payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            raise ForgedTokenError("Invalid token signature")

    async def bind_channel(
        self,
        conn: aiosqlite.Connection,
        actor_user_id: int,
        channel: str,
        address: str | int,
        token: str,
        ttl_seconds: int = 600,
    ) -> bool:
        self.verify_token(
            token, expected_user_id=actor_user_id, expected_channel=channel, ttl_seconds=ttl_seconds
        )
        user = await (
            await conn.execute("SELECT status FROM users WHERE id = ?", (actor_user_id,))
        ).fetchone()
        if not user or user[0] != "active":
            raise BindingError("Channel binding requires an active user")
        nonce = token.split("_")[3]
        sql = (
            "INSERT INTO channel_destinations "
            "(user_id, channel, address, verified, version, updated_at) "
            "VALUES (?, ?, ?, 1, 1, datetime('now')) "
            "ON CONFLICT(user_id, channel) DO UPDATE SET "
            "address = excluded.address, verified = 1, "
            "version = version + 1, updated_at = excluded.updated_at"
        )
        await conn.execute("SAVEPOINT consume_binding_nonce")
        try:
            consumed = await conn.execute(
                "INSERT OR IGNORE INTO binding_consumed_nonces "
                "(nonce, user_id, channel) VALUES (?, ?, ?)",
                (nonce, actor_user_id, channel),
            )
            if consumed.rowcount != 1:
                raise TokenReplayError("Binding token has already been used")
            await conn.execute(sql, (actor_user_id, channel, str(address)))
            await conn.execute("RELEASE SAVEPOINT consume_binding_nonce")
        except Exception:
            await conn.execute("ROLLBACK TO SAVEPOINT consume_binding_nonce")
            await conn.execute("RELEASE SAVEPOINT consume_binding_nonce")
            raise
        return True

    def extract_token(self, text: str) -> str | None:
        for word in text.strip().split():
            if word.startswith("bnd_") and len(word.split("_")) == 5:
                return word
        return None

    async def bind_from_peer(
        self,
        conn: aiosqlite.Connection,
        peer_id: int,
        token: str,
        expected_actor_user_id: int | None = None,
        ttl_seconds: int = 600,
    ) -> bool:
        if peer_id <= 0 or peer_id >= 2000000000:
            raise BindingError(f"Arbitrary or non-user peer rejected: {peer_id}")
        parts = token.split("_")
        if len(parts) != 5 or not parts[1].isdigit():
            raise ForgedTokenError("Malformed binding token")
        token_uid = int(parts[1])
        if expected_actor_user_id is not None and token_uid != expected_actor_user_id:
            raise CrossUserBindingError(
                f"Cross-user binding denied: token for {token_uid}, actor {expected_actor_user_id}"
            )
        return await self.bind_channel(conn, token_uid, "vk", str(peer_id), token, ttl_seconds)

    async def block_or_revoke_destination(
        self, conn: aiosqlite.Connection, user_id: int, channel: str = "vk"
    ) -> bool:
        cur = await conn.execute(
            "UPDATE channel_destinations SET verified = 0, updated_at = datetime('now') "
            "WHERE user_id = ? AND channel = ?",
            (user_id, channel),
        )
        await conn.commit()
        return cur.rowcount > 0

    async def is_destination_verified(
        self, conn: aiosqlite.Connection, user_id: int, channel: str = "vk"
    ) -> bool:
        cur = await conn.execute(
            "SELECT verified FROM channel_destinations WHERE user_id = ? AND channel = ?",
            (user_id, channel),
        )
        row = await cur.fetchone()
        return bool(row and row[0] == 1)
