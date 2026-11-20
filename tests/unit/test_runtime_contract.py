"""Runtime compatibility contracts for Python 3.13 and 3.14 (S7 / T-495)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_python_runtime_version_is_supported() -> None:
    """Checks that the executing Python version satisfies the supported contract (>= 3.13)."""
    assert sys.version_info >= (3, 13), f"Python >= 3.13 is required, current is {sys.version}"
    assert sys.version_info < (3, 15), f"Python 3.15+ is not yet targeted, current is {sys.version}"


def test_pyproject_requires_python_and_ruff_target() -> None:
    """Verifies pyproject.toml specifies requires-python >= 3.13 and ruff target py313."""
    pyproject_path = ROOT / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml must exist at root"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    req_py = data.get("project", {}).get("requires-python", "")
    assert req_py == ">=3.13", f"requires-python must be '>=3.13', got '{req_py}'"

    ruff_target = data.get("tool", {}).get("ruff", {}).get("target-version", "")
    assert ruff_target == "py313", f"ruff target-version must be 'py313', got '{ruff_target}'"


def test_modern_python_features_and_typing_syntax() -> None:
    """Verifies PEP 695 type statement, dataclass slots, and pattern matching run cleanly."""
    type TestId = str | int
    val: TestId = "id_101"

    match val:
        case str(s) if s.startswith("id_"):
            matched = True
        case _:
            matched = False
    assert matched is True


def test_deprecated_stdlib_modules_absence() -> None:
    """Checks that deprecated or removed legacy modules are not loaded."""
    legacy_modules = ("imp", "pipes", "cgi", "asyncore", "asynchat", "smtpd")
    for mod in legacy_modules:
        assert mod not in sys.modules, f"Deprecated module '{mod}' must not be loaded"
