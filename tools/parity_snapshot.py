#!/usr/bin/env python3
"""Offline parity snapshot runner and verifier.

Records and verifies deterministic JSON snapshots of the pure decision subsystems:
1. SERP parsing
2. Pricing and valuation
3. Model matching
4. Stage 0 / Round 1 fast filters
5. Final verdict / Judge
6. Message formatting
7. Handler roles and permissions
8. Schedule and budget allocation
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

# Add project root and src to sys.path so hunter package imports succeed seamlessly
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

from tools.parity.common import dump_json_snapshot
from tools.parity.subsystems import (
    evaluate_final_verdict_judge,
    evaluate_handler_roles,
    evaluate_message_formatting,
    evaluate_model_matching,
    evaluate_pricing_valuation,
    evaluate_schedule_budget,
    evaluate_serp_parsing,
    evaluate_stage0_round1,
)

PARITY_DIR = ROOT_DIR / "docs/arch/parity"

SUBSYSTEMS: list[tuple[str, Callable[[], Any | Awaitable[Any]]]] = [
    ("01_serp_parsing.json", evaluate_serp_parsing),
    ("02_pricing_valuation.json", evaluate_pricing_valuation),
    ("03_model_matching.json", evaluate_model_matching),
    ("04_stage0_round1.json", evaluate_stage0_round1),
    ("05_final_verdict_judge.json", evaluate_final_verdict_judge),
    ("06_message_formatting.json", evaluate_message_formatting),
    ("07_handler_roles.json", evaluate_handler_roles),
    ("08_schedule_budget.json", evaluate_schedule_budget),
]


async def _run_evaluator(evaluator: Callable[[], Any | Awaitable[Any]]) -> Any:
    """Execute evaluator whether synchronous or asynchronous."""
    res = evaluator()
    if asyncio.iscoroutine(res):
        return await res
    return res


async def run_record() -> int:
    """Record all parity snapshots to docs/arch/parity/."""
    PARITY_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Recording parity snapshots into {PARITY_DIR.relative_to(ROOT_DIR)}...")

    for filename, fn in SUBSYSTEMS:
        target = PARITY_DIR / filename
        data = await _run_evaluator(fn)
        dump_json_snapshot(data, target)
        print(f"  [RECORDED] {filename}")

    print("Parity snapshot recording completed successfully.")
    return 0


async def run_check() -> int:
    """Verify in-memory evaluations against saved snapshots."""
    print(f"Checking parity snapshots in {PARITY_DIR.relative_to(ROOT_DIR)}...")
    all_match = True

    for filename, fn in SUBSYSTEMS:
        target = PARITY_DIR / filename
        if not target.exists():
            print(f"  [MISSING] {filename} does not exist. Run 'record' first.", file=sys.stderr)
            all_match = False
            continue

        existing_text = target.read_text(encoding="utf-8")
        current_data = await _run_evaluator(fn)
        current_text = json.dumps(current_data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

        if existing_text == current_text:
            print(f"  [OK] {filename}")
        else:
            print(f"  [MISMATCH] {filename} differs!", file=sys.stderr)
            diff = difflib.unified_diff(
                existing_text.splitlines(keepends=True),
                current_text.splitlines(keepends=True),
                fromfile=f"saved/{filename}",
                tofile=f"computed/{filename}",
                n=3,
            )
            sys.stderr.writelines(diff)
            all_match = False

    if all_match:
        print("\nAll parity snapshots MATCH! Pure decision behavior is verified.")
        return 0

    print("\nParity check FAILED. Differences detected.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Record or check pure decision parity snapshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("record", help="Record baseline snapshots")
    subparsers.add_parser("check", help="Verify current decisions match baseline snapshots")

    args = parser.parse_args()
    if args.command == "record":
        return asyncio.run(run_record())
    if args.command == "check":
        return asyncio.run(run_check())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
