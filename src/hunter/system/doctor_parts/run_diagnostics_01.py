from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hunter.core.config import Settings
    from hunter.core.db import Database
    from hunter.system.doctor import DiagnosticCheck


async def check_database_integrity(
    db: Database | None,
    settings: Settings,
) -> tuple[DiagnosticCheck, bool]:
    from hunter.system.doctor import DiagnosticCheck

    try:
        conn = await db.connect() if db else None
        if conn:
            cur = await conn.execute("PRAGMA integrity_check;")
            res = await cur.fetchall()
            ok_check = any(row[0] == "ok" for row in res) if res else False

            cur_pages = await conn.execute("PRAGMA page_count;")
            pages = (await cur_pages.fetchone())[0]
            cur_psize = await conn.execute("PRAGMA page_size;")
            psize = (await cur_psize.fetchone())[0]
            db_size_mb = round((pages * psize) / (1024 * 1024), 2)

            wal_path = Path(str(settings.db_path) + "-wal")
            wal_size_mb = round(wal_path.stat().st_size / (1024 * 1024), 2) if wal_path.exists() else 0.0

            cur_free = await conn.execute("PRAGMA freelist_count;")
            free_pages = (await cur_free.fetchone())[0]

            if ok_check:
                return (
                    DiagnosticCheck(
                        name="database_integrity",
                        status="ok",
                        message=f"SQLite integrity verified (size: {db_size_mb} MB, WAL: {wal_size_mb} MB)",
                        details={"db_size_mb": db_size_mb, "wal_size_mb": wal_size_mb, "freelist_pages": free_pages},
                    ),
                    True,
                )
            return (
                DiagnosticCheck(
                    name="database_integrity",
                    status="fail",
                    message="SQLite integrity check failed!",
                    details={"errors": [r[0] for r in res]},
                ),
                False,
            )
        return (
            DiagnosticCheck(name="database_integrity", status="warn", message="DB connection not provided"),
            True,
        )
    except Exception as exc:
        return (
            DiagnosticCheck(name="database_integrity", status="fail", message=f"DB error: {exc}"),
            False,
        )


def check_disk_space(settings: Settings) -> tuple[DiagnosticCheck, bool]:
    from hunter.system.doctor import DiagnosticCheck

    try:
        data_path = settings.data_dir
        stat = os.statvfs(data_path)
        free_bytes = stat.f_bavail * stat.f_frsize
        free_gb = round(free_bytes / (1024 ** 3), 1)
        if free_gb < 5.0:
            return (
                DiagnosticCheck(name="disk_space", status="fail", message=f"Critically low disk space: {free_gb} GB free"),
                False,
            )
        if free_gb < 20.0:
            return (
                DiagnosticCheck(name="disk_space", status="warn", message=f"Low disk space: {free_gb} GB free"),
                True,
            )
        return (
            DiagnosticCheck(name="disk_space", status="ok", message=f"{free_gb} GB free on data disk"),
            True,
        )
    except Exception as exc:
        return (
            DiagnosticCheck(name="disk_space", status="warn", message=f"Could not check disk space: {exc}"),
            True,
        )



def check_backup_retention(
    settings: Settings,
    backups_dir: Path | None = None,
) -> DiagnosticCheck:
    from hunter.system.doctor import DiagnosticCheck

    try:
        b_dir = backups_dir or (settings.data_dir.parent / "backups")
        if not b_dir.exists():
            b_dir = settings.data_dir / "backups"
        if b_dir.exists():
            backups = [p for p in b_dir.glob("backup_*") if p.is_dir()]
            count = len(backups)
            if count >= 7:
                return DiagnosticCheck(
                    name="backup_retention",
                    status="ok",
                    message=f"{count} automated backups retained (policy minimum: 7)",
                    details={"count": count, "dir": str(b_dir)},
                )
            return DiagnosticCheck(
                name="backup_retention",
                status="warn",
                message=f"Only {count} backups found (expected at least 7)",
                details={"count": count, "dir": str(b_dir)},
            )
        return DiagnosticCheck(
            name="backup_retention",
            status="warn",
            message="No backups directory found",
            details={"checked_dir": str(b_dir)},
        )
    except Exception as exc:
        return DiagnosticCheck(name="backup_retention", status="warn", message=f"Failed to check backups: {exc}")


def check_ai_credentials(settings: Settings) -> DiagnosticCheck:
    from hunter.system.doctor import DiagnosticCheck

    has_gemini = bool(getattr(settings, "gemini_api_key", None))
    has_giga = bool(getattr(settings, "gigachat_credentials", None))
    if has_gemini and has_giga:
        return DiagnosticCheck(
            name="ai_credentials",
            status="ok",
            message="Gemini and GigaChat credentials active (VLM failover ready)",
        )
    if has_gemini:
        return DiagnosticCheck(
            name="ai_credentials",
            status="ok",
            message="Gemini API credentials active (GigaChat failover not configured)",
        )
    return DiagnosticCheck(
        name="ai_credentials",
        status="warn",
        message="No AI provider credentials configured (visual verification off)",
    )


async def check_circuit_breaker(
    db: Database | None,
    settings: Settings,
) -> tuple[DiagnosticCheck | None, bool]:
    from datetime import datetime
    from hunter.core.models import now_utc
    from hunter.system.doctor import DiagnosticCheck

    if not db:
        return None, True
    try:
        circuit_until = await db.get_runtime("circuit_until")
        paused = await db.is_paused()
        pause_flag = (settings.data_dir / "pause.flag").exists()
        captcha_flag = (settings.data_dir / "captcha.flag").exists()

        if circuit_until and datetime.fromisoformat(circuit_until) > now_utc():
            return (
                DiagnosticCheck(
                    name="circuit_breaker",
                    status="fail",
                    message=f"Circuit breaker ACTIVE until {circuit_until}",
                ),
                False,
            )
        if paused or pause_flag or captcha_flag:
            return (
                DiagnosticCheck(
                    name="circuit_breaker",
                    status="warn",
                    message=f"Collector is currently paused (paused={paused}, captcha={captcha_flag})",
                ),
                True,
            )
        return (
            DiagnosticCheck(
                name="circuit_breaker",
                status="ok",
                message="Circuit breaker clear, collector active",
            ),
            True,
        )
    except Exception as exc:
        return (
            DiagnosticCheck(name="circuit_breaker", status="warn", message=f"Could not check circuit: {exc}"),
            True,
        )
