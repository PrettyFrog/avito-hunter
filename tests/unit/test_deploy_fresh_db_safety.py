import os
import subprocess
from pathlib import Path


def test_legacy_fresh_db_mode_is_removed_before_any_deploy_action(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    script_path = root / "ops/deploy.sh"
    script = script_path.read_text()

    assert "--fresh-db" not in script
    assert "--confirm-wipe" not in script
    assert "pre_wipe" not in script
    assert "seed-from-legacy" not in script
    assert 'cp -p "$TARGET_DIR/data/hunter.db"' not in script

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh_called = fake_bin / "ssh-called"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(f"#!/bin/sh\ntouch {ssh_called}\n")
    fake_ssh.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(script_path), "--fresh-db"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Unknown option" in result.stderr
    assert not ssh_called.exists()
    assert not Path(env["HOME"], ".cache/avito-hunter/maintenance").exists()
