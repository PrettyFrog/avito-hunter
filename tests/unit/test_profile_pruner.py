from __future__ import annotations

import os
import time

from hunter.transport.browser_factory import _prune_stale_profiles


def test_prune_stale_profiles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    old_profile = tmp_path / "playwright_firefoxdev_profile-12345"
    old_profile.mkdir()
    (old_profile / "cookies.sqlite").write_text("dummy")

    recent_profile = tmp_path / "playwright_firefoxdev_profile-recent"
    recent_profile.mkdir()

    old_camoufox = tmp_path / "camoufox_tmp_profile"
    old_camoufox.mkdir()

    unrelated_dir = tmp_path / "other_old_dir"
    unrelated_dir.mkdir()

    past_time = time.time() - 250.0
    os.utime(str(old_profile), (past_time, past_time))
    os.utime(str(old_camoufox), (past_time, past_time))
    os.utime(str(unrelated_dir), (past_time, past_time))

    _prune_stale_profiles(max_age_sec=180.0)

    assert not old_profile.exists()
    assert not old_camoufox.exists()
    assert recent_profile.exists()
    assert unrelated_dir.exists()
