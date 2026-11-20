"""Admin handler tests must isolate the shared production rate limiter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_admin_support_limiter_isolation(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    code = """
import sys
import pytest
from hunter.notify.security import GLOBAL_STRANGER_LIMITER as limiter

assert limiter.user_rate_limit == 60
for _ in range(60):
    assert limiter.check_user_rate(1001)
assert not limiter.check_user_rate(1001)
result = pytest.main([
    "-q",
    "tests/unit/test_multiuser_m5_admin_support.py::test_admin_errors_callback_and_command",
    "--basetemp", sys.argv[1],
])
if result:
    raise SystemExit(result)
for _ in range(60):
    assert limiter.check_user_rate(1001)
assert not limiter.check_user_rate(1001)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "child-pytest")],
        cwd=root,
        env={**os.environ, "HUNTER_DATA_DIR": str(data_dir), "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
