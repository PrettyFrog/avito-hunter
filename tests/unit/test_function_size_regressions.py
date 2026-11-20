"""Focused AST guards for functions that must stay below the architecture limit."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _top_level_function_lengths(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: node.end_lineno - node.lineno + 1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_public_add_flow_functions_stay_within_architecture_limit() -> None:
    path = ROOT / "src/hunter/notify/telegram_handlers/public_add.py"
    lengths = _top_level_function_lengths(path)
    targets = (
        "_get_public_subscription_service",
        "_finish_public_add",
        "_register_public_cancel_command",
        "_register_public_add_cancel_callback",
        "_register_public_add_skip_callback",
        "register_public_add_callbacks",
    )
    violations = {name: lengths[name] for name in targets if lengths[name] > 60}
    assert not violations, f"Public add functions exceed 60 lines: {violations}"


def test_recipient_fanout_stays_within_architecture_limit() -> None:
    path = ROOT / "src/hunter/storage/recipient_fanout.py"
    lengths = _top_level_function_lengths(path)
    targets = ("apply_recipient_fanout", "_get_eligible_recipients")
    violations = {name: lengths[name] for name in targets if lengths[name] > 60}
    assert not violations, f"Recipient fanout functions exceed 60 lines: {violations}"


def test_dialog_text_handlers_stay_within_architecture_limit() -> None:
    path = (
        ROOT / "src/hunter/notify/telegram_router_parts/setup_telegram_router_on_dialog_text_201.py"
    )
    lengths = _top_level_function_lengths(path)
    targets = (
        "_handle_add_verification_failed",
        "handle_dialog_add_verification_steps",
        "_handle_dialog_early_waiting_steps",
        "handle_dialog_text_message",
    )
    violations = {name: lengths[name] for name in targets if lengths[name] > 60}
    assert not violations, f"Dialog text handlers exceed 60 lines: {violations}"
