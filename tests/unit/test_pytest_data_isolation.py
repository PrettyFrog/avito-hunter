import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.fixtures.data_sandbox import create_test_data_sandbox


@pytest.mark.parametrize("configured", [False, True])
def test_pytest_never_inherits_operational_data_dir(tmp_path: Path, configured: bool) -> None:
    root = Path(__file__).resolve().parents[2]
    operational = tmp_path / "operational"
    operational.mkdir()
    (operational / "hunter.db").write_text("sentinel")
    capture = tmp_path / "capture"
    script = '''
import os
from pathlib import Path
import pytest
class Probe:
    def pytest_sessionstart(self, session):
        Path(os.environ["TEST_CAPTURE"]).write_text(os.environ.get("HUNTER_DATA_DIR", ""))
args = ["--collect-only", "-q", "tests/unit/test_slot_session_redaction.py"]
previous = os.environ.get("HUNTER_DATA_DIR")
status = pytest.main(args, plugins=[Probe()])
assert os.environ.get("HUNTER_DATA_DIR") == previous
raise SystemExit(status)
'''
    env = dict(os.environ, TEST_CAPTURE=str(capture), PYTHONPATH=str(root / "src"))
    if configured:
        env["HUNTER_DATA_DIR"] = str(operational)
    else:
        env.pop("HUNTER_DATA_DIR", None)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = capture.read_text()
    assert observed and Path(observed).resolve() != operational.resolve()
    assert not Path(observed).exists()
    assert (operational / "hunter.db").read_text() == "sentinel"


def test_sandbox_copies_only_tracked_products_and_rejects_symlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    products = repo / "data/products"
    products.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (products / "fixture.yaml").write_text("fixture: true")
    subprocess.run(["git", "add", "data/products/fixture.yaml"], cwd=repo, check=True)
    (products / "untracked-secret").write_text("secret")
    (repo / "data/hunter.db").write_text("database")
    sandbox = create_test_data_sandbox(repo)
    try:
        copied = Path(sandbox.name)
        assert (copied / "products/fixture.yaml").read_text() == "fixture: true"
        assert not (copied / "products/untracked-secret").exists()
        assert not (copied / "hunter.db").exists()
    finally:
        sandbox.cleanup()

    (products / "linked").symlink_to(products / "fixture.yaml")
    subprocess.run(["git", "add", "data/products/linked"], cwd=repo, check=True)
    with pytest.raises(RuntimeError, match="Unsafe product fixture path"):
        create_test_data_sandbox(repo)


def test_sandbox_rejects_symlink_in_parent_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    products = repo / "data/products"
    tracked_dir = products / "tracked"
    tracked_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (tracked_dir / "fixture.yaml").write_text("tracked")
    subprocess.run(["git", "add", "data/products/tracked/fixture.yaml"], cwd=repo, check=True)
    (tracked_dir / "fixture.yaml").unlink()
    tracked_dir.rmdir()
    untracked_dir = products / "untracked"
    untracked_dir.mkdir()
    (untracked_dir / "fixture.yaml").write_text("private")
    tracked_dir.symlink_to(untracked_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Unsafe product fixture path"):
        create_test_data_sandbox(repo)
