import os
import subprocess
from pathlib import Path


def test_rollback_restores_code_without_touching_application_data(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    script_path = root / "ops/rollback.sh"
    script = script_path.read_text()

    assert "pre_wipe" not in script
    assert "--restore-db" not in script
    assert "products.tar.gz" not in script
    assert "presets.tar.gz" not in script
    assert "cp -p" not in script
    assert "sudo systemctl stop" in script
    assert "sudo systemctl start" in script
    assert "systemctl --user" not in script
    assert 'DEFAULT_TARGET="${AVITO_HUNTER_TARGET:-/opt/avito-hunter}"' in script
    assert 'HOST="${AVITO_HUNTER_HOST:-}"' in script
    assert 'rm -rf "$TARGET/\\$d"' not in script
    assert 'rm -rf "$TARGET/src"' in script
    assert 'mv "\\$RESTORE_STAGE/src" "$TARGET/src"' in script

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ssh_called = fake_bin / "ssh-called"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(f"#!/bin/sh\ntouch {ssh_called}\n")
    fake_ssh.chmod(0o755)
    env = os.environ.copy()
    env.pop("AVITO_HUNTER_HOST", None)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(script_path), "--no-restart"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "refusing to guess a machine" in result.stderr
    assert not ssh_called.exists()
