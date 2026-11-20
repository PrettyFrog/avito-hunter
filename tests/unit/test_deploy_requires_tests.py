import shutil
import subprocess
from pathlib import Path


def test_deploy_refuses_to_continue_without_offline_test_runner(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    shutil.copy2(
        Path(__file__).resolve().parents[2] / "ops/deploy.sh",
        repo / "ops/deploy.sh",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}

    result = subprocess.run(
        ["bash", str(repo / "ops/deploy.sh"), "--dry-run"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "pytest runner unavailable; refusing deployment" in result.stderr
    assert "ssh " not in result.stdout
    assert not (home / ".cache/avito-hunter/maintenance").exists()


def test_deploy_has_no_skip_tests_bypass_and_fails_closed_without_runner() -> None:
    script = (Path(__file__).resolve().parents[2] / "ops/deploy.sh").read_text()

    assert "--skip-tests" not in script
    assert "SKIP_TESTS" not in script
    assert "pytest not found, skipping" not in script
    assert 'echo "ERROR: pytest runner unavailable; refusing deployment." >&2' in script
