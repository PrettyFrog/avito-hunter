from __future__ import annotations

from pathlib import Path

import pytest
from tools import check_prod_patterns as checker
from tools.check_prod_patterns import scan_project


def _project(tmp_path: Path, source: str = "", tests: str = "", deploy: str = "") -> Path:
    (tmp_path / "src/hunter").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "ops").mkdir()
    (tmp_path / "src/hunter/sample.py").write_text(source, encoding="utf-8")
    (tmp_path / "tests/test_sample.py").write_text(tests, encoding="utf-8")
    (tmp_path / "ops/deploy.sh").write_text(deploy, encoding="utf-8")
    return tmp_path


def _assert_cli_rejects(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "ROOT", root)
    assert checker.main() == 1


@pytest.mark.parametrize(
    "source,expected",
    [
        ("from unittest.mock import Mock\nif isinstance(value, Mock):\n    pass\n", "mock"),
        ("try:\n    work()\nexcept:\n    pass\n", "bare except"),
        ("try:\n    work()\nexcept Exception:\n    pass\n", "except: pass"),
        ("try:\n    work()\nexcept (ValueError, OSError):\n    pass\n", "except: pass"),
        ("try:\n    work()\nexcept ValueError:\n    pass\n", "except: pass"),
        ("send(chat_id=12345)\n", "identity ID"),
        ("def send(owner_chat_id=12345):\n    return None\n", "identity ID"),
    ],
)
def test_production_antipatterns_are_rejected(
    tmp_path: Path, source: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, source=source)
    findings = scan_project(root)
    assert any(expected in finding.message for finding in findings)
    _assert_cli_rejects(root, monkeypatch)


@pytest.mark.parametrize(
    "test_source",
    [
        "import pytest\n@pytest.mark.xfail(reason='known')\ndef test_bad():\n    pass\n",
        "import pytest\ndef test_bad():\n    pytest.skip('local only')\n",
        """import pytest
@pytest.mark.xfail(
    strict=True,
    reason="ISSUE:X EXPIRES:2000-01-01 REVIEW:test",
)
def test_old():
    pass
""",
    ],
)
def test_untracked_skip_and_xfail_are_rejected(
    tmp_path: Path, test_source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, tests=test_source)
    findings = scan_project(root)
    assert findings
    _assert_cli_rejects(root, monkeypatch)


def test_deployment_skip_switch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, deploy="deploy --skip-tests\n")
    findings = scan_project(root)
    assert any("test bypass" in finding.message for finding in findings)
    _assert_cli_rejects(root, monkeypatch)


@pytest.mark.parametrize(
    "secret_source",
    [
        "bot_token = '123456789:" + "A" * 35 + "'\n",
        "api_key = 'AIza" + "B" * 35 + "'\n",
        "access_key = '" + "AKIA" + "1234567890ABCDEF" + "'\n",
        "private = '-----BEGIN " + "RSA PRIVATE KEY-----'\n",
        "api_key = '" + "C" * 32 + "'\n",
        "endpoint = 'https://relay:" + "D" * 16 + "@example.test'\n",
    ],
)
def test_embedded_secret_is_rejected_without_printing_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    secret_source: str,
) -> None:
    root = _project(tmp_path, source=secret_source)
    _assert_cli_rejects(root, monkeypatch)
    captured = capsys.readouterr()
    assert "possible embedded secret" in captured.err
    assert secret_source.split("'")[1] not in captured.err


def test_reviewed_future_skip_metadata_is_accepted(tmp_path: Path) -> None:
    source = """import pytest
@pytest.mark.xfail(
    strict=True,
    reason="ISSUE:TEST-1 EXPIRES:2099-12-31 REVIEW:maintainer",
)
def test_expected_failure():
    raise RuntimeError
"""
    assert scan_project(_project(tmp_path, tests=source)) == []
