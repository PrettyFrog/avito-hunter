"""Database slice 50: Digest queue management, LLM call telemetry, and photo cache."""

from collections.abc import Awaitable, Callable
import json
from typing import Any
import aiosqlite

from hunter.core.models import now_utc, utcnow


async def get_queued_digest_items_db(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Retrieve all pending items from digest queue."""
    cur = await conn.execute("SELECT key, value FROM runtime WHERE key LIKE 'digest_queue:%'")
    rows = await cur.fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        try:
            val = json.loads(r["value"])
            if isinstance(val, dict):
                val["_digest_key"] = r["key"]
                items.append(val)
        except Exception:
            continue
    return items


async def clear_digest_queue_db(conn: aiosqlite.Connection, keys: list[str] | None = None) -> None:
    """Clear items from digest queue. If keys provided, only delete those keys."""
    if keys is not None:
        if not keys:
            return
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            placeholders = ",".join("?" for _ in chunk)
            await conn.execute(f"DELETE FROM runtime WHERE key IN ({placeholders})", chunk)
    else:
        await conn.execute("DELETE FROM runtime WHERE key LIKE 'digest_queue:%'")
    await conn.commit()


async def log_llm_call_db(
    conn: aiosqlite.Connection,
    *,
    provider: str,
    model: str,
    call_type: str,
    product_id: str | None = None,
    avito_id: str | None = None,
    tokens_prompt: int = 0,
    tokens_completion: int = 0,
    tokens_total: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
    error_msg: str | None = None,
) -> None:
    """Log LLM invocation telemetry into llm_calls table."""
    now_str = now_utc().isoformat()
    await conn.execute(
        """
        INSERT INTO llm_calls (
            ts, provider, model, call_type, product_id, avito_id,
            tokens_prompt, tokens_completion, tokens_total,
            latency_ms, status, error_msg, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_str, provider, model, call_type, product_id, avito_id,
            tokens_prompt, tokens_completion, tokens_total,
            latency_ms, status, error_msg, now_str,
        ),
    )
    await conn.commit()


async def get_cached_photo_check_db(
    conn: aiosqlite.Connection,
    photo_sha256: str,
    sig_hash: str,
    method: str,
    provider: str,
) -> dict[str, Any] | None:
    """Fetch cached photo check verification result."""
    cur = await conn.execute(
        """
        SELECT result_json FROM photo_cache
        WHERE photo_sha256 = ? AND sig_hash = ? AND method = ? AND provider = ?
        """,
        (photo_sha256, sig_hash, method, provider),
    )
    row = await cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["result_json"])
    except Exception:
        return None


async def save_cached_photo_check_db(
    conn: aiosqlite.Connection,
    photo_sha256: str,
    sig_hash: str,
    method: str,
    provider: str,
    result: dict[str, Any],
) -> None:
    """Save photo check verification result into photo_cache table."""
    now_str = now_utc().isoformat()
    res_json = json.dumps(result, ensure_ascii=False)
    await conn.execute(
        """
        INSERT OR REPLACE INTO photo_cache (
            photo_sha256, sig_hash, method, provider, result_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (photo_sha256, sig_hash, method, provider, res_json, now_str),
    )
    await conn.commit()


async def get_cached_photo_check_v2_db(
    conn: aiosqlite.Connection,
    photo_hash: str,
    signature_version: str,
    prompt_version: str,
    method: str,
    provider: str,
) -> dict[str, Any] | None:
    """Fetch cached photo check v2 verification result."""
    cur = await conn.execute(
        """
        SELECT result_json FROM photo_checks
        WHERE photo_hash = ? AND signature_version = ? AND prompt_version = ? AND method = ? AND provider = ?
        ORDER BY id DESC LIMIT 1
        """,
        (photo_hash, signature_version, prompt_version, method, provider),
    )
    row = await cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["result_json"])
    except Exception:
        return None


async def save_photo_check_db(
    conn: aiosqlite.Connection,
    avito_id: str,
    product_id: str,
    method: str,
    photo_hash: str,
    signature_version: str,
    prompt_version: str,
    provider: str,
    model_version: str,
    result: dict[str, Any],
) -> None:
    """Save photo check execution row to photo_checks table."""
    res_json = json.dumps(result, ensure_ascii=False)
    await conn.execute(
        """
        INSERT INTO photo_checks (
            avito_id, product_id, method, photo_hash, signature_version, prompt_version, provider, model_version, result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            avito_id,
            product_id,
            method,
            photo_hash,
            signature_version,
            prompt_version,
            provider,
            model_version,
            res_json,
        ),
    )
    await conn.commit()


async def today_product_photo_checks_db(
    get_runtime: Callable[[str, str], Awaitable[str]],
    product_id: str,
) -> int:
    """Count photo checks performed today for a product."""
    day = utcnow()[:10]
    val = await get_runtime(f"product_photo_checks:{product_id}:{day}", "0")
    return int(val) if val and val.isdigit() else 0


async def bump_product_photo_checks_db(
    get_runtime: Callable[[str, str], Awaitable[str]],
    set_runtime: Callable[[str, str], Awaitable[None]],
    product_id: str,
) -> int:
    """Increment photo checks performed today for a product."""
    day = utcnow()[:10]
    cur = await today_product_photo_checks_db(get_runtime, product_id)
    new_val = cur + 1
    await set_runtime(f"product_photo_checks:{product_id}:{day}", str(new_val))
    return new_val

