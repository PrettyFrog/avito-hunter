"""Deployment preflight must report config drift without printing values."""
import os
import shutil
import subprocess
from pathlib import Path


def _script(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def test_deploy_config_drift_never_prints_secret_values(tmp_path):
    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    shutil.copy2(root / "ops/deploy.sh", repo / "ops/deploy.sh")
    (repo / "config").mkdir()
    (repo / "config/settings.yaml").write_text("relay_token: local-private-value\n")
    _script(repo / ".venv/bin/pytest", "#!/bin/sh\nexit 0\n")
    _script(repo / ".venv/bin/hunter", "#!/bin/sh\nexit 0\n")
    fake_bin = tmp_path / "bin"
    _script(fake_bin / "git", "#!/bin/sh\nexit 0\n")
    _script(fake_bin / "ssh", '''#!/bin/sh
case "$*" in
  *SSH_OK*) exit 0 ;;
  *PY_OK*) printf 'PY_OK 3.13\\n'; exit 0 ;;
  *"cat '"*"config/settings.yaml"*) printf 'relay_token: remote-private-value\\n'; exit 0 ;;
  *deploy_manifest*) printf 'YES\\n'; exit 0 ;;
  *) exit 71 ;;
esac
''')
    result = subprocess.run(
        ["bash", str(repo / "ops/deploy.sh"), "--dry-run"], cwd=repo,
        env={**os.environ, "HOME": str(tmp_path / "home"), "PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    assert "Configuration changes detected in config/settings.yaml" in output
    assert "local-private-value" not in output
    assert "remote-private-value" not in output
    assert result.returncode == 71  # Deliberately stop at the next fake SSH step.
