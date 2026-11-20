"""Architecture size ratchet and import dependency rules guards (S0 / T-011).

Ensures:
- Files > 400 lines are ratcheted by baseline (cannot grow).
- Functions > 60 lines are ratcheted by baseline (cannot grow).
- All new files are strictly <= 400 lines, all new functions <= 60 lines.
- Domain/leaf modules do not import application/UI layers.
- Uses pure AST inspection without importing production runtime.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from tools.size_report import scan_directory

ROOT = Path(__file__).resolve().parent.parent.parent
TARGET_DIR = ROOT / "src/hunter"
ARCH_SIZES_FILE = ROOT / "tests/fixtures/arch_sizes.json"

_V2_FILE_OVERRIDES: dict[str, int] = {
    "src/hunter/cli.py": 400,
    "src/hunter/collect/browser.py": 400,
    "src/hunter/core/db.py": 400,
    "src/hunter/notify/telegram_router.py": 400,
    "src/hunter/pipeline/orchestrator.py": 400,
}

_V2_FUNC_OVERRIDES: dict[str, int] = {
    "src/hunter/audit/skips.py::audit_skips": 60,
    "src/hunter/cli.py::_run": 117,
    "src/hunter/collect/browser.py::BrowserSession.evaluate_in_tab_fetch": 60,
    "src/hunter/collect/browser.py::BrowserSession._init_secondary_profiles": 60,
    "src/hunter/collect/browser.py::BrowserSession.start": 60,
    "src/hunter/collect/browser.py::BrowserSession.fetch": 60,
    "src/hunter/collect/lightfetch.py::fetch_v1": 60,
    "src/hunter/collect/lightfetch.py::fetch_v2": 60,
    "src/hunter/collect/technical.py::parse_show_technical": 60,
    "src/hunter/core/seed.py::seed_from_legacy": 60,
    "src/hunter/domain/slots.py::transition_slot_health": 78,
    "src/hunter/notify/telegram_router.py::setup_telegram_router": 903,
    "src/hunter/pipeline/orchestrator.py::Orchestrator._poll_watch": 250,
    "src/hunter/pipeline/orchestrator.py::Orchestrator._handle_hit": 185,
    "src/hunter/pipeline/orchestrator.py::Orchestrator._notify": 125,
    "src/hunter/storage/job_claims.py::JobClaimRepository.claim_job": 71,
    "src/hunter/system/doctor.py::run_diagnostics": 60,
    "src/hunter/system/report.py::generate_daily_report": 60,
    "src/hunter/ui/app.py::create_app": 60,
}


def _load_baseline() -> dict[str, Any]:
    data = json.loads(ARCH_SIZES_FILE.read_text(encoding="utf-8"))
    data["baseline_files"].update(_V2_FILE_OVERRIDES)
    data["baseline_functions"].update(_V2_FUNC_OVERRIDES)
    return data


def test_file_size_ratchet_and_limit():
    """Verify that no file exceeds 400 lines unless grandfathered, and baseline files do \
not grow."""
    baseline = _load_baseline()
    base_files = baseline["baseline_files"]
    max_lines = baseline.get("max_file_lines", 400)

    stats = scan_directory(TARGET_DIR, ROOT)
    errors: list[str] = []

    for f in stats:
        rel = f.file_path
        if rel in base_files:
            allowed = base_files[rel]
            if f.line_count > allowed:
                errors.append(
                    f"{rel}: grew from baseline {allowed} to {f.line_count} lines "
                    f"(+{f.line_count - allowed})"
                )
        else:
            if f.line_count > max_lines:
                errors.append(
                    f"{rel}: non-baseline file exceeds {max_lines} lines ({f.line_count} lines)"
                )

    assert not errors, "File size ratchet violations:\n" + "\n".join(errors)


def test_function_size_ratchet_and_limit():
    """Verify that no function exceeds 60 lines unless grandfathered, and baseline \
functions do not grow."""
    baseline = _load_baseline()
    base_funcs = baseline["baseline_functions"]
    max_func_lines = baseline.get("max_func_lines", 60)

    stats = scan_directory(TARGET_DIR, ROOT)
    errors: list[str] = []

    for f in stats:
        for fn in f.functions:
            key = f"{fn.file_path}::{fn.name}"
            if key in base_funcs:
                allowed = base_funcs[key]
                if fn.line_count > allowed:
                    errors.append(
                        f"{key}: grew from baseline {allowed} to {fn.line_count} lines "
                        f"(+{fn.line_count - allowed})"
                    )
            else:
                if fn.line_count > max_func_lines:
                    errors.append(
                        f"{key}: non-baseline function exceeds {max_func_lines} lines "
                        f"({fn.line_count} lines)"
                    )

    assert not errors, "Function size ratchet violations:\n" + "\n".join(errors)


def test_all_hunter_modules_obey_file_and_function_limits():
    """F-13: enforce the 400-line module and 60-line function limits across src/hunter."""
    stats = scan_directory(TARGET_DIR, ROOT)
    errors = [
        f"{item.file_path}: {item.line_count} lines" for item in stats if item.line_count > 400
    ]
    errors.extend(
        f"{item.file_path}::{fn.name}: {fn.line_count} lines"
        for item in stats
        for fn in item.functions
        if fn.line_count > 60
    )
    assert not errors, "F-13 architecture limits exceeded:\n" + "\n".join(errors)


def test_domain_layer_dependency_rules():
    """Verify via pure AST that core domain modules do not import application / UI layers."""
    forbidden_prefixes = (
        "hunter.notify.telegram_router",
        "hunter.pipeline.orchestrator",
        "hunter.cli",
        "aiogram",
        "apscheduler",
    )
    domain_roots = [
        ROOT / "src/hunter/core/models.py",
        ROOT / "src/hunter/extract",
        ROOT / "src/hunter/matching",
    ]

    domain_files: list[Path] = []
    for dr in domain_roots:
        if dr.is_file():
            domain_files.append(dr)
        elif dr.is_dir():
            domain_files.extend(dr.rglob("*.py"))

    violations: list[str] = []
    for py_file in domain_files:
        if "__pycache__" in py_file.parts:
            continue
        rel = py_file.relative_to(ROOT)
        content = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(content, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(p) for p in forbidden_prefixes):
                        violations.append(f"{rel}:{node.lineno} imports '{alias.name}'")
            elif isinstance(node, ast.ImportFrom):
                if node.module and any(node.module.startswith(p) for p in forbidden_prefixes):
                    violations.append(f"{rel}:{node.lineno} imports from '{node.module}'")

    assert not violations, "Domain dependency rule violations:\n" + "\n".join(violations)
























def _assert_facade_and_parts(facade_rel: str, parts_prefix: str, min_parts: int = 1) -> None:
    stats = scan_directory(TARGET_DIR, ROOT)
    f_stat = [s for s in stats if s.file_path == facade_rel]
    assert f_stat and f_stat[0].line_count <= 400, f"{facade_rel} missing or > 400"
    for fn in f_stat[0].functions:
        assert fn.line_count <= 60, f"{facade_rel}::{fn.name} is {fn.line_count} > 60"
    parts = [s for s in stats if s.file_path.startswith(parts_prefix)]
    assert len(parts) >= min_parts, f"Expected >= {min_parts} parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"
















def test_collect_parsers_search_facade_and_parts_limits():
    """T-044..T-056: Verify search.py facade and search_parts modules <=400 lines \
and leaf functions <=60."""
    _assert_facade_and_parts(
        "src/hunter/collect/parsers/search.py", "src/hunter/collect/parsers/search_parts/", 6
    )


def test_collect_parsers_mfe_state_facade_and_parts_limits():
    """T-057..T-060: Verify mfe_state.py facade and mfe_state_parts <=400 lines \
and functions <=60."""
    _assert_facade_and_parts(
        "src/hunter/collect/parsers/mfe_state.py", "src/hunter/collect/parsers/mfe_state_parts/", 2
    )


def test_collect_parsers_card_facade_and_parts_limits():
    """T-061..T-064: Verify card.py facade and card_parts modules <=400 lines and \
functions <=60."""
    _assert_facade_and_parts(
        "src/hunter/collect/parsers/card.py",
        "src/hunter/collect/parsers/card_parts/",
        1,
    )


def test_notify_messages_ru_facade_and_parts_limits():
    """T-065: Check architecture limits."""
    _assert_facade_and_parts(
        "src/hunter/notify/messages_ru.py", "src/hunter/notify/messages_ru_parts/", 3
    )


def test_notify_base_facade_and_parts_complete_limits():
    """T-072: Check architecture limits."""
    _assert_facade_and_parts("src/hunter/notify/base.py", "src/hunter/notify/base_parts/", 1)






























































































def test_llm_router_llmerror_01_limits():
    """T-132: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmerror_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_llm_router_complete_json_02_limits():
    """T-133: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmerror_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_llm_router_gigachat_03_limits():
    """T-134: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmerror_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_llm_router_gemini_04_limits():
    """T-135: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gemini_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_llm_router_gemini_vision_05_limits():
    """T-136: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gemini_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_llm_router_complete_vision_json_07_limits():
    """T-137: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gemini_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    router_match = [
        s
        for s in scan_directory(TARGET_DIR / "llm", ROOT)
        if s.file_path == "src/hunter/llm/router.py"
    ]
    cvj = [fn for fn in router_match[0].functions if fn.name == "LLMRouter.complete_vision_json"]
    assert cvj and cvj[0].line_count <= 60


def test_llm_router_gigachat_vision_08_limits():
    """T-138: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gigachat_vision_08.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    router_match = [
        s
        for s in scan_directory(TARGET_DIR / "llm", ROOT)
        if s.file_path == "src/hunter/llm/router.py"
    ]
    assert (
        router_match
        and router_match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in router_match[0].functions)
    )


def test_llm_router_complete_vision_dual_10_limits():
    """T-139: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gigachat_vision_08.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    router_match = [
        s
        for s in scan_directory(TARGET_DIR / "llm", ROOT)
        if s.file_path == "src/hunter/llm/router.py"
    ]
    assert (
        router_match
        and router_match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in router_match[0].functions)
    )


def test_llm_router_gemini_vision_11_limits():
    """T-140: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gigachat_vision_08.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    router_match = [
        s
        for s in scan_directory(TARGET_DIR / "llm", ROOT)
        if s.file_path == "src/hunter/llm/router.py"
    ]
    assert (
        router_match
        and router_match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in router_match[0].functions)
    )


def test_llm_router_gemini_vision_12_limits():
    """T-141: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gemini_vision_12.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    router_match = [
        s
        for s in scan_directory(TARGET_DIR / "llm", ROOT)
        if s.file_path == "src/hunter/llm/router.py"
    ]
    assert (
        router_match
        and router_match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in router_match[0].functions)
    )


def test_llm_router_gemini_vision_13_limits():
    """T-142: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "llm/router_parts", ROOT)
        if s.file_path == "src/hunter/llm/router_parts/llmrouter__gemini_vision_12.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    router_match = [
        s
        for s in scan_directory(TARGET_DIR / "llm", ROOT)
        if s.file_path == "src/hunter/llm/router.py"
    ]
    assert (
        router_match
        and router_match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in router_match[0].functions)
    )


def test_llm_router_facade_and_parts_limits():
    """T-143: Check architecture size limits."""
    _assert_facade_and_parts("src/hunter/llm/router.py", "src/hunter/llm/router_parts/", 3)










































def test_core_product_productconflict_01_limits():
    """T-164: Check architecture limits for the Telegram module."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/productconflict_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_normalize_search_url_02_limits():
    """T-165: Check architecture limits for the Telegram module."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/productconflict_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_to_watch_item_03_limits():
    """T-166: Check architecture limits for the Telegram module."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/productconflict_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_to_watch_04_limits():
    """T-167: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/productprofile_to_watch_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_clear_product_pause_05_limits():
    """T-168: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/productprofile_to_watch_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_delete_product_profile_06_limits():
    """T-169: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/productprofile_to_watch_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_delete_user_products_07_limits():
    """T-170: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/delete_user_products_07.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_product_store_init_08_limits():
    """T-171: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/delete_user_products_07.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_product_store_load_all_09_limits():
    """T-172: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/delete_user_products_07.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_product_store_alert_validation_error_10_limits():
    """T-173: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/product_parts", ROOT)
        if s.file_path == "src/hunter/core/product_parts/productstore__alert_validation_error_10.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_product_facade_and_parts_limits():
    """T-174: Check architecture size limits."""
    _assert_facade_and_parts("src/hunter/core/product.py", "src/hunter/core/product_parts/", 4)


def test_core_db_schema_path_01_limits():
    """T-175: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/schema_path_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_02_limits():
    """T-176: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/schema_path_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_03_limits():
    """T-177: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/schema_path_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_04_limits():
    """T-178: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_05_limits():
    """T-179: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_04.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_06_limits():
    """T-180: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_06.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_07_limits():
    """T-181: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_06.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_08_limits():
    """T-182: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_08.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_09_limits():
    """T-183: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_08.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_10_limits():
    """T-184: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_10.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_11_limits():
    """T-185: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_10.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_migrate_12_limits():
    """T-186: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_migrate_10.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_checkpoint_13_limits():
    """T-187: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_checkpoint_13.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_set_runtime_14_limits():
    """T-188: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_checkpoint_13.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_create_user_15_limits():
    """T-189: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_checkpoint_13.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_user_by_telegram_id_16_limits():
    """T-190: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_user_by_telegram_id_16.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_delete_user_data_17_limits():
    """T-191: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_user_by_telegram_id_16.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_block_user_by_owner_19_limits():
    """T-192: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_block_user_by_owner_19.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_open_support_tickets_20_limits():
    """T-193: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_block_user_by_owner_19.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_create_invite_21_limits():
    """T-194: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_create_invite_21.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_active_invites_count_22_limits():
    """T-195: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_create_invite_21.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_resolve_access_request_atomic_23_limits():
    """T-196: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_resolve_access_request_atomic_23.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_alerts_by_tier_24_limits():
    """T-197: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_resolve_access_request_atomic_23.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_pop_digest_items_for_user_25_limits():
    """T-198: Check architecture size limits."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_pop_digest_items_for_user_25.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_last_user_notification_26_limits():
    """T-199: Verify core/db_parts/database_pop_digest_items_for_user_25.py slice \
26 <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_pop_digest_items_for_user_25.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_recent_notification_by_fingerprint_27_limits():
    """T-200: Verify core/db_parts/database_pop_digest_items_for_user_25.py slice \
27 <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_pop_digest_items_for_user_25.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_daily_cap_with_source_28_limits():
    """T-201: Verify core/db_parts/database_get_daily_cap_with_source_28.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_daily_cap_with_source_28.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_rolling_24h_requests_29_limits():
    """T-202: Verify core/db_parts/database_get_daily_cap_with_source_28.py slice \
29 <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_daily_cap_with_source_28.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_record_daily_health_30_limits():
    """T-203: Verify core/db_parts/database_get_daily_cap_with_source_28.py slice \
30 <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_daily_cap_with_source_28.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_check_p90_spike_32_limits():
    """T-204: Verify core/db_parts/database_check_p90_spike_32.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_check_p90_spike_32.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_record_feedback_entry_33_limits():
    """T-205: Verify core/db_parts/database_check_p90_spike_32.py slice 33 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_check_p90_spike_32.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_today_requests_34_limits():
    """T-206: Verify core/db_parts/database_today_requests_34.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_today_requests_34.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_upsert_listing_35_limits():
    """T-207: Verify core/db_parts/database_today_requests_34.py slice 35 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_today_requests_34.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_upsert_listing_36_limits():
    """T-208: Verify core/db_parts/database_today_requests_34.py slice 36 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_today_requests_34.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_touch_listing_37_limits():
    """T-209: Verify core/db_parts/database_touch_listing_37.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_touch_listing_37.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_save_extraction_38_limits():
    """T-210: Verify core/db_parts/database_touch_listing_37.py slice 38 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_touch_listing_37.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_pending_notifications_39_limits():
    """T-211: Verify core/db_parts/database_get_pending_notifications_39.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_pending_notifications_39.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_upsert_seller_40_limits():
    """T-212: Verify core/db_parts/database_get_pending_notifications_39.py slice \
40 <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_pending_notifications_39.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_sync_products_41_limits():
    """T-213: Verify core/db_parts/database_sync_products_41.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_sync_products_41.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_find_duplicate_images_42_limits():
    """T-214: Verify core/db_parts/database_sync_products_41.py slice 42 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_sync_products_41.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_pending_vision_43_limits():
    """T-215: Verify core/db_parts/database_sync_products_41.py slice 43 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_sync_products_41.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_recover_leases_44_limits():
    """T-216: Verify core/db_parts/database_recover_leases_44.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_recover_leases_44.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_lease_job_45_limits():
    """T-217: Verify core/db_parts/database_recover_leases_44.py slice 45 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_recover_leases_44.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_complete_job_46_limits():
    """T-218: Verify core/db_parts/database_recover_leases_44.py slice 46 <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_recover_leases_44.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_poison_jobs_summary_47_limits():
    """T-219: Verify core/db_parts/database_get_poison_jobs_summary_47.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_poison_jobs_summary_47.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_recent_request_errors_48_limits():
    """T-220: Verify core/db_parts/database_get_poison_jobs_summary_47.py slice 48 \
<=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_poison_jobs_summary_47.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_sync_product_profiles_49_limits():
    """T-221: Verify core/db_parts/database_get_poison_jobs_summary_47.py slice 49 \
<=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_poison_jobs_summary_47.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_queued_digest_items_50_limits():
    """T-222: Verify core/db_parts/database_get_queued_digest_items_50.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_queued_digest_items_50.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_cached_photo_check_v2_51_limits():
    """T-223: Verify core/db_parts/database_get_queued_digest_items_50.py slice 51 \
<=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_queued_digest_items_50.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_save_photo_shadow_result_52_limits():
    """T-224: Verify core/db_parts/database_save_photo_shadow_result_52.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_save_photo_shadow_result_52.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_count_today_photo_shadow_results_53_limits():
    """T-225: Verify core/db_parts/database_save_photo_shadow_result_52.py slice \
53 <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_save_photo_shadow_result_52.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_get_recent_shadow_overlaps_54_limits():
    """T-226: Verify core/db_parts/database_get_recent_shadow_overlaps_54.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/db_parts", ROOT)
        if s.file_path == "src/hunter/core/db_parts/database_get_recent_shadow_overlaps_54.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_db_facade_and_parts_limits():
    """T-227: Verify core/db.py functions <=60 and all db_parts/ modules <=400 \
lines and functions <=60."""
    stats = scan_directory(TARGET_DIR, ROOT)
    db_stat = [s for s in stats if s.file_path == "src/hunter/core/db.py"]
    assert db_stat, "src/hunter/core/db.py missing"
    for fn in db_stat[0].functions:
        assert fn.line_count <= 60, f"src/hunter/core/db.py::{fn.name} is {fn.line_count} > 60"
    parts = [s for s in stats if s.file_path.startswith("src/hunter/core/db_parts/")]
    assert len(parts) >= 20, f"Expected >= 20 db_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_core_seed_from_legacy_01_limits():
    """T-230: Verify core/seed_parts/seed_from_legacy_01.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/seed_parts", ROOT)
        if s.file_path == "src/hunter/core/seed_parts/seed_from_legacy_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_seed_from_legacy_03_limits():
    """T-232: Verify core/seed_parts/seed_from_legacy_03.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "core/seed_parts", ROOT)
        if s.file_path == "src/hunter/core/seed_parts/seed_from_legacy_03.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_core_seed_facade_and_parts_limits():
    """T-233: Verify core/seed.py <=400 lines and all functions <=60, and seed_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    seed_stat = [s for s in stats if s.file_path == "src/hunter/core/seed.py"]
    assert seed_stat, "src/hunter/core/seed.py missing"
    assert seed_stat[0].line_count <= 400
    for fn in seed_stat[0].functions:
        assert fn.line_count <= 60, f"src/hunter/core/seed.py::{fn.name} is {fn.line_count} > 60"
    parts = [s for s in stats if s.file_path.startswith("src/hunter/core/seed_parts/")]
    assert len(parts) >= 2, f"Expected >= 2 seed_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_collect_technical_parts_parse_show_technical_01_limits():
    """T-234: Verify collect/technical_parts/parse_show_technical_01.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/technical_parts", ROOT)
        if s.file_path == "src/hunter/collect/technical_parts/parse_show_technical_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_technical_facade_and_parts_limits():
    """T-235: Verify collect/technical.py <=400 lines and all functions <=60, and \
technical_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    tech_stat = [s for s in stats if s.file_path == "src/hunter/collect/technical.py"]
    assert tech_stat, "src/hunter/collect/technical.py missing"
    assert tech_stat[0].line_count <= 400
    for fn in tech_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/collect/technical.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/collect/technical_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 technical_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_collect_lightfetch_parts_v1_fetch_script_01_limits():
    """T-236: Verify collect/lightfetch_parts/v1_fetch_script_01.py <=400 lines \
and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/lightfetch_parts", ROOT)
        if s.file_path == "src/hunter/collect/lightfetch_parts/v1_fetch_script_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_lightfetch_parts_check_block_markers_05_limits():
    """T-239: Verify collect/lightfetch_parts/check_block_markers_05.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/lightfetch_parts", ROOT)
        if s.file_path == "src/hunter/collect/lightfetch_parts/check_block_markers_05.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_lightfetch_facade_and_parts_limits():
    """T-242: Verify collect/lightfetch.py <=400 lines and all functions <=60, and \
lightfetch_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    lf_stat = [s for s in stats if s.file_path == "src/hunter/collect/lightfetch.py"]
    assert lf_stat, "src/hunter/collect/lightfetch.py missing"
    assert lf_stat[0].line_count <= 400
    for fn in lf_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/collect/lightfetch.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/collect/lightfetch_parts/")]
    assert len(parts) >= 2, f"Expected >= 2 lightfetch_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_collect_browser_parts_is_target_closed_error_01_limits():
    """T-243: Verify collect/browser_parts/is_target_closed_error_01.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/browser_parts", ROOT)
        if s.file_path == "src/hunter/collect/browser_parts/is_target_closed_error_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_browser_parts_browsersession__ensure_default_profile_03_limits():
    """T-245/T-246/T-247: Verify \
collect/browser_parts/browsersession__ensure_default_profile_03.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/browser_parts", ROOT)
        if s.file_path
        == "src/hunter/collect/browser_parts/browsersession__ensure_default_profile_03.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_browser_parts_browsersession_get_or_create_light_page_06_limits():
    """T-248/T-249: Verify \
collect/browser_parts/browsersession_get_or_create_light_page_06.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/browser_parts", ROOT)
        if s.file_path
        == "src/hunter/collect/browser_parts/browsersession_get_or_create_light_page_06.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_browser_parts_browsersession__init_secondary_profiles_08_limits():
    """T-250/T-251: Verify \
collect/browser_parts/browsersession__init_secondary_profiles_08.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/browser_parts", ROOT)
        if s.file_path
        == "src/hunter/collect/browser_parts/browsersession__init_secondary_profiles_08.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_browser_parts_browsersession_start_11_limits():
    """T-252/T-253/T-254: Verify collect/browser_parts/browsersession_start_11.py \
<=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/browser_parts", ROOT)
        if s.file_path == "src/hunter/collect/browser_parts/browsersession_start_11.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_browser_parts_browsersession_fetch_14_limits():
    """T-255: Verify collect/browser_parts/browsersession_fetch_14.py <=400 lines \
and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/browser_parts", ROOT)
        if s.file_path == "src/hunter/collect/browser_parts/browsersession_fetch_14.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_browser_facade_and_parts_limits():
    """T-256: Verify collect/browser.py <=400 lines and all functions <=60, and \
browser_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    b_stat = [s for s in stats if s.file_path == "src/hunter/collect/browser.py"]
    assert b_stat, "src/hunter/collect/browser.py missing"
    assert b_stat[0].line_count <= 400
    for fn in b_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/collect/browser.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/collect/browser_parts/")]
    assert len(parts) >= 6, f"Expected >= 6 browser_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_collect_politeness_parts_before_request_01_limits():
    """T-258/T-259: Verify \
collect/politeness_parts/politeness_before_request_01.py <=400 lines and \
functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/politeness_parts", ROOT)
        if s.file_path == "src/hunter/collect/politeness_parts/politeness_before_request_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    p_match = [
        s
        for s in scan_directory(TARGET_DIR / "collect", ROOT)
        if s.file_path == "src/hunter/collect/politeness.py"
    ]
    assert p_match
    br_fn = [fn for fn in p_match[0].functions if fn.name == "Politeness.before_request"]
    assert br_fn and br_fn[0].line_count <= 60, (
        f"Politeness.before_request is {br_fn[0].line_count} > 60"
    )


def test_collect_politeness_facade_and_parts_limits():
    """T-260: Verify collect/politeness.py <=400 lines and all functions <=60, and \
politeness_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    p_stat = [s for s in stats if s.file_path == "src/hunter/collect/politeness.py"]
    assert p_stat, "src/hunter/collect/politeness.py missing"
    assert p_stat[0].line_count <= 400
    for fn in p_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/collect/politeness.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/collect/politeness_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 politeness_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_collect_search_parts_fetch_search_light_01_limits():
    """T-261: Verify collect/search_parts/fetch_search_light_01.py <=400 lines and \
functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "collect/search_parts", ROOT)
        if s.file_path == "src/hunter/collect/search_parts/fetch_search_light_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_collect_search_facade_and_parts_limits():
    """T-262: Verify collect/search.py <=400 lines and all functions <=60, and \
search_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    s_stat = [s for s in stats if s.file_path == "src/hunter/collect/search.py"]
    assert s_stat, "src/hunter/collect/search.py missing"
    assert s_stat[0].line_count <= 400
    for fn in s_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/collect/search.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/collect/search_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 search_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_notify_telegram_parts_telegramchannel_send_01_limits():
    """T-263/T-264: Verify notify/telegram_parts/telegramchannel_send_01.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "notify/telegram_parts", ROOT)
        if s.file_path == "src/hunter/notify/telegram_parts/telegramchannel_send_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )
    t_match = [
        s
        for s in scan_directory(TARGET_DIR / "notify", ROOT)
        if s.file_path == "src/hunter/notify/telegram.py"
    ]
    assert t_match
    send_fn = [fn for fn in t_match[0].functions if fn.name == "TelegramChannel.send"]
    assert send_fn and send_fn[0].line_count <= 60, (
        f"TelegramChannel.send is {send_fn[0].line_count} > 60"
    )


def test_notify_telegram_facade_and_parts_limits():
    """T-265: Verify notify/telegram.py <=400 lines and all functions <=60, and \
telegram_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    t_stat = [s for s in stats if s.file_path == "src/hunter/notify/telegram.py"]
    assert t_stat, "src/hunter/notify/telegram.py missing"
    assert t_stat[0].line_count <= 400
    for fn in t_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/notify/telegram.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/notify/telegram_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 telegram_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_notify_vk_parts_vknotifier_send_notification_01_limits():
    """T-266: Verify notify/vk_parts/vknotifier_send_notification_01.py <=400 \
lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "notify/vk_parts", ROOT)
        if s.file_path == "src/hunter/notify/vk_parts/vknotifier_send_notification_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_notify_vk_facade_and_parts_limits():
    """T-267: Verify notify/vk.py <=400 lines and all functions <=60, and vk_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    v_stat = [s for s in stats if s.file_path == "src/hunter/notify/vk.py"]
    assert v_stat, "src/hunter/notify/vk.py missing"
    assert v_stat[0].line_count <= 400
    for fn in v_stat[0].functions:
        assert fn.line_count <= 60, f"src/hunter/notify/vk.py::{fn.name} is {fn.line_count} > 60"
    parts = [s for s in stats if s.file_path.startswith("src/hunter/notify/vk_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 vk_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_notify_broadcast_parts_send_broadcast_01_limits():
    """T-269: Verify notify/broadcast_parts/send_broadcast_01.py <=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "notify/broadcast_parts", ROOT)
        if s.file_path == "src/hunter/notify/broadcast_parts/send_broadcast_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_notify_broadcast_facade_and_parts_limits():
    """T-270: Verify notify/broadcast.py <=400 lines and all functions <=60, and \
broadcast_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    b_stat = [s for s in stats if s.file_path == "src/hunter/notify/broadcast.py"]
    assert b_stat, "src/hunter/notify/broadcast.py missing"
    assert b_stat[0].line_count <= 400
    for fn in b_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/notify/broadcast.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/notify/broadcast_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 broadcast_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_notify_security_parts_authandscopemiddleware___call_01_limits():
    """T-271: Verify notify/security_parts/authandscopemiddleware___call_01.py \
<=400 lines and functions <=60."""
    match = [
        s
        for s in scan_directory(TARGET_DIR / "notify/security_parts", ROOT)
        if s.file_path == "src/hunter/notify/security_parts/authandscopemiddleware___call_01.py"
    ]
    assert (
        match
        and match[0].line_count <= 400
        and all(fn.line_count <= 60 for fn in match[0].functions)
    )


def test_notify_security_facade_and_parts_limits():
    """T-274: Verify notify/security.py <=400 lines and all functions <=60, and \
security_parts <=400."""
    stats = scan_directory(TARGET_DIR, ROOT)
    s_stat = [s for s in stats if s.file_path == "src/hunter/notify/security.py"]
    assert s_stat, "src/hunter/notify/security.py missing"
    assert s_stat[0].line_count <= 400
    for fn in s_stat[0].functions:
        assert fn.line_count <= 60, (
            f"src/hunter/notify/security.py::{fn.name} is {fn.line_count} > 60"
        )
    parts = [s for s in stats if s.file_path.startswith("src/hunter/notify/security_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 security_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"






















































































































































def test_telegram_router_parts_originalproductstore_01_limits():
    """T-352: Verify telegram_router_parts/originalproductstore_01.py <=400 lines and \
functions <=60."""
    part_file = TARGET_DIR / "notify/telegram_router_parts/originalproductstore_01.py"
    assert part_file.exists(), f"{part_file} does not exist"
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path == "src/hunter/notify/telegram_router_parts/originalproductstore_01.py"
    ]
    assert match, "originalproductstore_01.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_registration_03_limits():
    """T-353: Verify telegram_router_parts/originalproductstore_01.py <=400 lines and \
functions <=60 after slice 03."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path == "src/hunter/notify/telegram_router_parts/originalproductstore_01.py"
    ]
    assert match, "originalproductstore_01.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_start_08_limits():
    """T-354: Verify telegram_router_parts/originalproductstore_01.py <=400 lines and \
functions <=60 after slice 08."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path == "src/hunter/notify/telegram_router_parts/originalproductstore_01.py"
    ]
    assert match, "originalproductstore_01.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_start_09_limits():
    """T-355: Verify telegram_router_parts/setup_telegram_router_cmd_start_09.py <=400 \
lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_start_09.py"
    ]
    assert match, "setup_telegram_router_cmd_start_09.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_req_access_11_limits():
    """T-356: Verify telegram_router_parts/setup_telegram_router_cmd_start_09.py <=400 \
lines and functions <=60 after cb_req_access."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_start_09.py"
    ]
    assert match, "setup_telegram_router_cmd_start_09.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_owner_acc_ok_13_limits():
    """T-357: Verify telegram_router_parts/setup_telegram_router_cb_owner_acc_ok_13.py \
<=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cb_owner_acc_ok_13.py"
    ]
    assert match, "setup_telegram_router_cb_owner_acc_ok_13.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_onboarding_16_limits():
    """T-358: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cb_owner_acc_ok_13.py"
    ]
    assert match, "setup_telegram_router_cb_owner_acc_ok_13.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_invite_23_limits():
    """T-359: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_invite_23.py"
    ]
    assert match, "setup_telegram_router_cmd_invite_23.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_revoke_inv_27_limits():
    """T-360: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_invite_23.py"
    ]
    assert match, "setup_telegram_router_cmd_invite_23.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_faq_menu_33_limits():
    """T-361: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cb_faq_menu_33.py"
    ]
    assert match, "setup_telegram_router_cb_faq_menu_33.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_tickets_and_broadcast_42_limits():
    """T-362: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cb_faq_menu_33.py"
    ]
    assert match, "setup_telegram_router_cb_faq_menu_33.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_delete_me_53_limits():
    """T-363: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_delete_me_53.py"
    ]
    assert match, "setup_telegram_router_cmd_delete_me_53.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_admin_users_view_63_limits():
    """T-364: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_delete_me_53.py"
    ]
    assert match, "setup_telegram_router_cmd_delete_me_53.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_admin_u_pause_69_limits():
    """T-365/T-366: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cb_admin_u_pause_69.py"
    ]
    assert match, "setup_telegram_router_cb_admin_u_pause_69.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_admin_u_del_confirm_85_limits():
    """T-367/T-368: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == (
            "src/hunter/notify/telegram_router_parts/"
            "setup_telegram_router_cb_admin_u_del_confirm_85.py"
        )
    ]
    assert match, "setup_telegram_router_cb_admin_u_del_confirm_85.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts__format_errors_report_98_limits():
    """T-369/T-370: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == (
            "src/hunter/notify/telegram_router_parts/"
            "setup_telegram_router__format_errors_report_98.py"
        )
    ]
    assert match, "setup_telegram_router__format_errors_report_98.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_my_pause_109_limits():
    """T-371/T-372: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cb_my_pause_109.py"
    ]
    assert match, "setup_telegram_router_cb_my_pause_109.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_found_125_limits():
    """T-373/T-374: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_found_125.py"
    ]
    assert match, "setup_telegram_router_cmd_found_125.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_registration_136_limits():
    """T-375/T-376/T-377: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_registration_136.py"
    ]
    assert match, "setup_telegram_router_registration_136.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_edit_154_limits():
    """T-378/T-379: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_edit_154.py"
    ]
    assert match, "setup_telegram_router_cmd_edit_154.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_handle_apply_stage_158_limits():
    """T-380/T-381: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_handle_apply_stage_158.py"
    ]
    assert match, "setup_telegram_router_handle_apply_stage_158.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_stats_166_limits():
    """T-382/T-383: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_stats_166.py"
    ]
    assert match, "setup_telegram_router_cmd_stats_166.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cmd_photocheck_174_limits():
    """T-384/T-385: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cmd_photocheck_174.py"
    ]
    assert match, "setup_telegram_router_cmd_photocheck_174.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_start_add_178_limits():
    """T-386/T-387: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_cb_start_add_178.py"
    ]
    assert match, "setup_telegram_router_cb_start_add_178.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_cb_add_confirm_launch_194_limits():
    """T-388/T-389: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == (
            "src/hunter/notify/telegram_router_parts/"
            "setup_telegram_router_cb_add_confirm_launch_194.py"
        )
    ]
    assert match, "setup_telegram_router_cb_add_confirm_launch_194.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_on_dialog_text_201_limits():
    """T-390/T-391: Check router part file and function limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_on_dialog_text_201.py"
    ]
    assert match, "setup_telegram_router_on_dialog_text_201.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_on_photo_attachment_204_limits():
    """T-392/T-393: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == (
            "src/hunter/notify/telegram_router_parts/"
            "setup_telegram_router_on_photo_attachment_204.py"
        )
    ]
    assert match, "setup_telegram_router_on_photo_attachment_204.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_on_alert_ref_add_208_limits():
    """T-394/T-395: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_on_alert_ref_add_208.py"
    ]
    assert match, "setup_telegram_router_on_alert_ref_add_208.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_on_audit_feedback_222_limits():
    """T-396/T-397: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/setup_telegram_router_on_audit_feedback_222.py"
    ]
    assert match, "setup_telegram_router_on_audit_feedback_222.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_format_product_card_234_limits():
    """T-398/T-399: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path == "src/hunter/notify/telegram_router_parts/format_product_card_234.py"
    ]
    assert match, "format_product_card_234.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_start_add_verification_user_237_limits():
    """T-400/T-401: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/start_add_verification_user_237.py"
    ]
    assert match, "start_add_verification_user_237.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_start_add_verification_user_239_limits():
    """T-402/T-403: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path
        == "src/hunter/notify/telegram_router_parts/start_add_verification_user_239.py"
    ]
    assert match, "start_add_verification_user_239.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_start_add_verification_241_limits():
    """T-404/T-405: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path == "src/hunter/notify/telegram_router_parts/start_add_verification_241.py"
    ]
    assert match, "start_add_verification_241.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_telegram_router_parts_render_summary_card_243_limits():
    """T-406: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "notify/telegram_router_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path == "src/hunter/notify/telegram_router_parts/render_summary_card_243.py"
    ]
    assert match, "render_summary_card_243.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_notify_telegram_router_facade_and_parts_limits():
    """T-407: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR, ROOT)
    tr_stat = [s for s in stats if s.file_path == "src/hunter/notify/telegram_router.py"]
    assert tr_stat, "src/hunter/notify/telegram_router.py missing"
    assert tr_stat[0].line_count <= 400, f"telegram_router.py is {tr_stat[0].line_count} > 400"
    for fn in tr_stat[0].functions:
        assert fn.line_count <= 60, f"telegram_router.py::{fn.name} has {fn.line_count} lines (>60)"
    parts = [s for s in stats if s.file_path.startswith("src/hunter/notify/telegram_router_parts/")]
    assert len(parts) >= 20, f"Expected >= 20 telegram_router_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_system_doctor_parts_run_diagnostics_01_limits():
    """T-408, T-409: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "system/doctor_parts", ROOT)
    match = [
        s for s in stats if s.file_path == "src/hunter/system/doctor_parts/run_diagnostics_01.py"
    ]
    assert match, "run_diagnostics_01.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_system_doctor_parts_run_diagnostics_03_limits():
    """T-410: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "system/doctor_parts", ROOT)
    match = [
        s for s in stats if s.file_path == "src/hunter/system/doctor_parts/run_diagnostics_03.py"
    ]
    assert match, "run_diagnostics_03.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_system_doctor_facade_and_parts_limits():
    """T-411: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR, ROOT)
    doc_stat = [s for s in stats if s.file_path == "src/hunter/system/doctor.py"]
    assert doc_stat, "src/hunter/system/doctor.py missing"
    assert doc_stat[0].line_count <= 400, f"doctor.py is {doc_stat[0].line_count} > 400"
    for fn in doc_stat[0].functions:
        assert fn.line_count <= 60, f"doctor.py::{fn.name} has {fn.line_count} lines (>60)"
    parts = [s for s in stats if s.file_path.startswith("src/hunter/system/doctor_parts/")]
    assert len(parts) >= 2, f"Expected >= 2 doctor_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_system_report_parts_generate_daily_report_01_limits():
    """T-412, T-413: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "system/report_parts", ROOT)
    match = [
        s
        for s in stats
        if s.file_path == "src/hunter/system/report_parts/generate_daily_report_01.py"
    ]
    assert match, "generate_daily_report_01.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_system_report_facade_and_parts_limits():
    """T-414: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR, ROOT)
    rep_stat = [s for s in stats if s.file_path == "src/hunter/system/report.py"]
    assert rep_stat, "src/hunter/system/report.py missing"
    assert rep_stat[0].line_count <= 400, f"report.py is {rep_stat[0].line_count} > 400"
    for fn in rep_stat[0].functions:
        assert fn.line_count <= 60, f"report.py::{fn.name} has {fn.line_count} lines (>60)"
    parts = [s for s in stats if s.file_path.startswith("src/hunter/system/report_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 report_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


def test_audit_skips_parts_audit_skips_01_limits():
    """T-415, T-416: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR / "audit/skips_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/audit/skips_parts/audit_skips_01.py"]
    assert match, "audit_skips_01.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_audit_skips_facade_and_parts_limits():
    """T-417: Enforce module and function size limits."""
    stats = scan_directory(TARGET_DIR, ROOT)
    sk_stat = [s for s in stats if s.file_path == "src/hunter/audit/skips.py"]
    assert sk_stat, "src/hunter/audit/skips.py missing"
    assert sk_stat[0].line_count <= 400, f"skips.py is {sk_stat[0].line_count} > 400"
    for fn in sk_stat[0].functions:
        assert fn.line_count <= 60, f"skips.py::{fn.name} has {fn.line_count} lines (>60)"
    parts = [s for s in stats if s.file_path.startswith("src/hunter/audit/skips_parts/")]
    assert len(parts) >= 1, f"Expected >= 1 skips_parts, got {len(parts)}"
    for part in parts:
        assert part.line_count <= 400, f"{part.file_path} is {part.line_count} > 400"
        for fn in part.functions:
            assert fn.line_count <= 60, f"{part.file_path}::{fn.name} is {fn.line_count} > 60"


















def test_cli_parts_app_01_limits():
    """T-432/T-433: Verify cli_parts/app_01.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/app_01.py"]
    assert match, "app_01.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_eval_05_limits():
    """T-434/T-435: Verify cli_parts/eval_05.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/eval_05.py"]
    assert match, "eval_05.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_round2_07_limits():
    """T-436/T-437: Verify cli_parts/round2_07.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/round2_07.py"]
    assert match, "round2_07.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_budget_cmd_09_limits():
    """T-438/T-439: Verify cli_parts/budget_cmd_09.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/budget_cmd_09.py"]
    assert match, "budget_cmd_09.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_budget_cmd_11_limits():
    """T-440/T-441: Verify cli_parts/budget_cmd_11.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/budget_cmd_11.py"]
    assert match, "budget_cmd_11.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_search_mode_14_limits():
    """T-442..T-444: Verify cli_parts/search_mode_14.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/search_mode_14.py"]
    assert match, "search_mode_14.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_near_miss_cmd_18_limits():
    """T-445..T-447: Verify cli_parts/near_miss_cmd_18.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/near_miss_cmd_18.py"]
    assert match, "near_miss_cmd_18.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_photo_calibrate_cmd_21_limits():
    """T-448/T-449/T-450: Verify cli_parts/photo_calibrate_cmd_21.py <=400 lines and \
functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/photo_calibrate_cmd_21.py"]
    assert match, "photo_calibrate_cmd_21.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_parts_shadow_probe_cmd_24_limits():
    """T-451/T-452: Verify cli_parts/shadow_probe_cmd_24.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR / "cli_parts", ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli_parts/shadow_probe_cmd_24.py"]
    assert match, "shadow_probe_cmd_24.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_cli_limits():
    """T-453: Verify cli.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR, ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/cli.py"]
    assert match, "cli.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"


def test_price_history_v2_storage_limits():
    """T-469 / T-496: Verify price_history.py <=400 lines and functions <=60."""
    stats = scan_directory(TARGET_DIR, ROOT)
    match = [s for s in stats if s.file_path == "src/hunter/storage/price_history.py"]
    assert match, "price_history.py not found in scan"
    assert match[0].line_count <= 400
    for fn in match[0].functions:
        assert fn.line_count <= 60, f"{fn.name} has {fn.line_count} lines (>60)"











































































































































