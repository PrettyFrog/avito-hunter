from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def test_ci_workflow_requires_offline_locked_quality_gates() -> None:
    config = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = config["jobs"]["offline-quality"]
    assert job["runs-on"] == "self-hosted"
    assert job["timeout-minutes"] >= 30
    steps = job["steps"]
    checkout = steps[0]
    assert checkout["uses"].endswith("11bd71901bbe5b1630ceea73d27597364c9af683")
    assert checkout["with"]["persist-credentials"] is False
    script = "\n".join(step.get("run", "") for step in steps)
    required = (
        "command -v uv",
        "UV_OFFLINE=1",
        "UV_PYTHON_DOWNLOADS=never",
        'cp -a data/products/. "$HUNTER_DATA_DIR/products/"',
        "uv lock --check",
        "uv sync --frozen --extra dev --no-install-project",
        "-m pip check",
        "tools/check_prod_patterns.py",
        "tools/codemap.py --check",
        "tools/parity_snapshot.py check",
        "test_architecture_limits.py",
        "-m pytest -q tests",
        "ruff\" check --select ANN",
        "docs/work/validate_prod_plan.py",
    )
    assert all(command in script for command in required)
    assert "--skip-tests" not in script
    assert "--runxfail" not in script
    assert "actions/setup-python" not in str(config)
