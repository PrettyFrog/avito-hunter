from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from hunter.core.models import now_utc


def get_process_memory_mb(pid: int | str = "self") -> tuple[float, float]:
    """Reads (VmRSS, VmSwap) in MB directly from /proc/<pid>/status without external dependencies."""
    rss_mb = 0.0
    swap_mb = 0.0
    try:
        path = Path(f"/proc/{pid}/status")
        if not path.exists():
            return 0.0, 0.0
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    rss_mb = round(float(parts[1]) / 1024.0, 1)
            elif line.startswith("VmSwap:"):
                parts = line.split()
                if len(parts) >= 2:
                    swap_mb = round(float(parts[1]) / 1024.0, 1)
    except Exception:
        pass
    return rss_mb, swap_mb


def get_process_rss_mb(pid: int | str = "self") -> float:
    """Reads VmRSS in MB directly from /proc/<pid>/status without external dependencies."""
    return get_process_memory_mb(pid)[0]


def get_cgroup_memory_mb() -> float:
    """Reads cgroup memory.current in MB for current process cgroup (cgroups v2)."""
    try:
        cg_text = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
        cg_rel = cg_text.split(":")[-1].lstrip("/")
        p = Path("/sys/fs/cgroup") / cg_rel / "memory.current"
        if p.exists():
            return round(int(p.read_text().strip()) / (1024.0 * 1024.0), 1)
        root_p = Path("/sys/fs/cgroup/memory.current")
        if root_p.exists():
            return round(int(root_p.read_text().strip()) / (1024.0 * 1024.0), 1)
    except Exception:
        pass
    return 0.0


def collect_memory_breakdown() -> dict[str, float]:
    """Collects RSS and Swap breakdown for current Python process, Playwright node driver, Chromium, and cgroup."""
    python_rss, python_swap = get_process_memory_mb("self")
    node_rss, node_swap = 0.0, 0.0
    chromium_rss, chromium_swap = 0.0, 0.0

    current_pid = os.getpid()
    try:
        # Find child or sibling processes belonging to hunter service
        proc_output = subprocess.check_output(
            ["pgrep", "-f", "playwright|chromium|chrome|hunter"],
            text=True,
            timeout=2,
        ).splitlines()
        for p_str in proc_output:
            p_str = p_str.strip()
            if not p_str:
                continue
            try:
                p_int = int(p_str)
                if p_int == current_pid:
                    continue
                cmdline = Path(f"/proc/{p_str}/cmdline").read_text(encoding="utf-8", errors="ignore")
                rss, swap = get_process_memory_mb(p_str)
                if "node" in cmdline or "driver" in cmdline:
                    node_rss += rss
                    node_swap += swap
                elif "chrome" in cmdline or "chromium" in cmdline:
                    chromium_rss += rss
                    chromium_swap += swap
                elif "hunter" in cmdline:
                    python_rss += rss
                    python_swap += swap
            except Exception:
                continue
    except Exception:
        pass

    total_rss = round(python_rss + node_rss + chromium_rss, 1)
    browser_rss_swap = round(chromium_rss + chromium_swap + node_rss + node_swap, 1)
    cgroup_mb = get_cgroup_memory_mb()
    return {
        "python_rss_mb": round(python_rss, 1),
        "python_swap_mb": round(python_swap, 1),
        "node_rss_mb": round(node_rss, 1),
        "node_swap_mb": round(node_swap, 1),
        "chromium_rss_mb": round(chromium_rss, 1),
        "chromium_swap_mb": round(chromium_swap, 1),
        "browser_rss_swap_mb": browser_rss_swap,
        "total_rss_mb": total_rss,
        "cgroup_memory_mb": cgroup_mb,
    }


async def record_memory_heartbeat(db: Any) -> dict[str, float]:
    """Records current memory stats into DB memory_log table and trims free heap."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

    stats = collect_memory_breakdown()
    try:
        if hasattr(db, "log_memory"):
            await db.log_memory(
                python_rss_mb=stats["python_rss_mb"],
                node_rss_mb=stats["node_rss_mb"],
                chromium_rss_mb=stats["chromium_rss_mb"],
                total_rss_mb=stats["total_rss_mb"],
            )
    except Exception as exc:
        logger.debug("Failed to record memory log: {}", exc)
    return stats
