import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(("runner_exit", "expected_exit"), [(42, 42), (0, 43)])
def test_deploy_tests_use_temporary_data_dir_and_abort_before_ssh(
    tmp_path: Path, runner_exit: int, expected_exit: int
) -> None:
    source = Path(__file__).resolve().parents[2] / "ops/deploy.sh"
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    shutil.copy2(source, repo / "ops/deploy.sh")
    products = repo / "data/products"
    products.mkdir(parents=True)
    (products / "fixture.yaml").write_text("fixture: true\n")
    fake_venv = repo / ".venv/bin"
    fake_venv.mkdir(parents=True)
    runner = fake_venv / "pytest"
    runner.write_text(
        '#!/bin/sh\n'
        'printf "%s" "$HUNTER_DATA_DIR" > "$TEST_CAPTURE"\n'
        'test -f "$HUNTER_DATA_DIR/products/fixture.yaml" || exit 89\n'
        'test ! -e "$HUNTER_DATA_DIR/hunter.db" || exit 90\n'
        'exit "$TEST_RUNNER_EXIT"\n'
    )
    runner.chmod(0o755)
    hunter = fake_venv / "hunter"
    hunter.write_text(
        '#!/bin/sh\nprintf "%s" "$HUNTER_DATA_DIR" > "$TEST_HUNTER_CAPTURE"\nexit 43\n'
    )
    hunter.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh = fake_bin / "ssh"
    ssh.write_text('#!/bin/sh\ntouch "$TEST_SSH_MARKER"\nexit 1\n')
    ssh.chmod(0o755)
    live = tmp_path / "live-data"
    live.mkdir()
    (live / "hunter.db").write_text("unchanged sentinel")
    capture = tmp_path / "captured"
    hunter_capture = tmp_path / "hunter-captured"
    marker = tmp_path / "ssh-called"
    env = dict(os.environ, HOME=str(tmp_path / "home"), PATH=f"{fake_bin}:/usr/bin:/bin")
    env.update(
        HUNTER_DATA_DIR=str(live), TEST_CAPTURE=str(capture), TEST_SSH_MARKER=str(marker),
        TEST_HUNTER_CAPTURE=str(hunter_capture), TEST_RUNNER_EXIT=str(runner_exit),
    )
    result = subprocess.run(
        ["bash", str(repo / "ops/deploy.sh"), "--dry-run"], cwd=repo, env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == expected_exit
    sandbox = Path(capture.read_text())
    assert sandbox != live and not sandbox.exists()
    assert (live / "hunter.db").read_text() == "unchanged sentinel"
    assert not marker.exists()
    if runner_exit == 0:
        assert hunter_capture.read_text() == str(sandbox)
    else:
        assert not hunter_capture.exists()
