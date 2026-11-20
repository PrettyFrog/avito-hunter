#!/usr/bin/env python3
"""Validates that changed files belong to the active ticket scope."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = ROOT / "docs/work/TASKS.md"
NEXT_PATH = ROOT / "docs/work/NEXT.md"


def get_current_ticket_id() -> str | None:
    if not NEXT_PATH.exists():
        return None
    for line in NEXT_PATH.read_text(encoding="utf-8").splitlines():
        m = re.search(r"Текущий тикет:\s*(T-\d+(?:\.[a-z])?)", line)
        if m:
            return m.group(1)
    return None


def get_ticket_whitelist(ticket_id: str) -> list[str] | None:
    text = TASKS_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^### {re.escape(ticket_id)}\b.*?\n(.*?)(?=^### T-|\Z)", re.M | re.S)
    m = pattern.search(text)
    if not m:
        return None
    block = m.group(1)
    files_m = re.search(r"^Файлы \(только эти\):\s*(.+)$", block, re.M)
    if not files_m:
        return None
    raw_files = files_m.group(1)
    # Extract backtick or path tokens
    paths = re.findall(r"`([^`]+)`", raw_files)
    if not paths:
        # Fallback to comma/space splitting without modifiers like (новый), (из T-xxx)
        cleaned = re.sub(r"\([^)]+\)", "", raw_files)
        paths = [p.strip() for p in cleaned.split(",") if p.strip()]
    return [p.strip() for p in paths if p.strip()]


def get_staged_or_commit_files(commit_ref: str | None = None) -> list[str]:
    cmd = ["git", "diff", "--name-only", commit_ref] if commit_ref else ["git", "diff", "--name-only", "--cached"]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return []
    return [f.strip() for f in res.stdout.splitlines() if f.strip()]


def is_allowed(path: str, whitelist: list[str]) -> bool:
    if path.startswith("tests/") or path.startswith("docs/work/") or path.startswith("docs/agent/"):
        return True
    if path in [".gitignore", "tools/work_status.py", "tools/check_ticket_scope.py"]:
        return True
    for w in whitelist:
        clean_w = w.split()[0].replace("`", "")
        if path == clean_w or path.endswith(clean_w):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check changed files vs ticket scope.")
    parser.add_argument("--ticket", type=str, help="Ticket ID (e.g. T-001)")
    parser.add_argument("--commit", type=str, help="Commit ref to inspect")
    args = parser.parse_args()

    tid = args.ticket or get_current_ticket_id()
    if not tid:
        print("WARNING: No ticket ID specified or found in NEXT.md; skipping check.")
        return 0

    whitelist = get_ticket_whitelist(tid)
    if whitelist is None:
        print(f"WARNING: Could not parse whitelist for ticket {tid}; skipping check.")
        return 0

    changed = get_staged_or_commit_files(args.commit)
    if not changed:
        return 0

    violations = [f for f in changed if not is_allowed(f, whitelist)]
    if violations:
        print(f"ERROR: Files outside ticket {tid} whitelist: {violations}", file=sys.stderr)
        print(f"Declared whitelist: {whitelist}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
