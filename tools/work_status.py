#!/usr/bin/env python3
"""Parses TASKS.md and generates PROGRESS.md and HEARTBEAT.txt."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = ROOT / "docs/work/TASKS.md"
PROGRESS_PATH = ROOT / "docs/work/PROGRESS.md"
HEARTBEAT_PATH = ROOT / "docs/work/HEARTBEAT.txt"


def main() -> int:
    text = TASKS_PATH.read_text(encoding="utf-8")
    t_pattern = re.compile(r"^### (T-\d+(?:\.[a-z])?)\s+\[([EMH])\]\s+(.*)$", re.M)
    status_pattern = re.compile(r"^Статус:\s*(\S+)", re.M)

    blocks = re.split(r"(?=^### T-\d+)", text, flags=re.M)
    tickets: list[dict[str, str]] = []
    for b in blocks:
        t_match = t_pattern.search(b)
        s_match = status_pattern.search(b)
        if t_match and s_match:
            tickets.append({
                "id": t_match.group(1),
                "type": t_match.group(2),
                "title": t_match.group(3).strip(),
                "status": s_match.group(1).strip().lower(),
            })

    total = len(tickets)
    by_status = Counter(t["status"] for t in tickets)
    by_type = Counter(f"{t['type']}:{t['status']}" for t in tickets)
    in_prog = [t["id"] for t in tickets if t["status"] == "in-progress"]
    curr_id = in_prog[0] if in_prog else (tickets[0]["id"] if tickets else "T-001")

    now_iso = datetime.now(UTC).isoformat()
    done = by_status.get("done", 0)
    blocked = by_status.get("blocked", 0)
    heartbeat_line = f"{now_iso} | {curr_id} | {done}/{total} | {blocked}\n"
    HEARTBEAT_PATH.write_text(heartbeat_line, encoding="utf-8")

    lines = [
        "# PROGRESS.md",
        "",
        f"- Обновлено: {now_iso}",
        f"- Всего тикетов: {total}",
        f"- Done: {done} ({(done/total*100) if total else 0:.1f}%)",
        f"- In-progress: {by_status.get('in-progress', 0)}",
        f"- Blocked: {blocked}",
        f"- Todo: {by_status.get('todo', 0)}",
        "",
        "## По типам задач",
        "",
        "| Тип | Всего | Done | In-Progress | Blocked | Todo |",
        "|-----|-------|------|-------------|---------|------|",
    ]
    for typ in ["E", "M", "H"]:
        t_tot = sum(1 for t in tickets if t["type"] == typ)
        t_d = by_type.get(f"{typ}:done", 0)
        t_ip = by_type.get(f"{typ}:in-progress", 0)
        t_b = by_type.get(f"{typ}:blocked", 0)
        t_td = by_type.get(f"{typ}:todo", 0)
        lines.append(f"| {typ} | {t_tot} | {t_d} | {t_ip} | {t_b} | {t_td} |")

    PROGRESS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Tickets: {total} total | {done} done | {by_status.get('in-progress', 0)} in-progress | {blocked} blocked | {by_status.get('todo', 0)} todo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
