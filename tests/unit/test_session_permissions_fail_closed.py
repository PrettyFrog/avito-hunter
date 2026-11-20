import os
import stat
from pathlib import Path

import pytest

from hunter.transport.session_store import SessionStore


def test_session_store_fails_when_directory_permissions_cannot_be_secured(tmp_path, monkeypatch):
    def deny_chmod(path, mode):
        raise PermissionError("simulated denial")

    monkeypatch.setattr(os, "chmod", deny_chmod)
    with pytest.raises(PermissionError):
        SessionStore(tmp_path / "sessions")


@pytest.mark.parametrize(
    ("file_mode", "dir_mode"), [(0o644, 0o700), (0o600, 0o755), (0o666, 0o777)]
)
def test_session_store_rejects_modes_exposing_secrets(tmp_path, file_mode, dir_mode):
    with pytest.raises(ValueError):
        SessionStore(tmp_path / "sessions", file_mode=file_mode, dir_mode=dir_mode)


@pytest.mark.asyncio
async def test_file_permission_failure_preserves_existing_session(tmp_path: Path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    await store.save_session(
        "slot_a", "http://proxy:8080", 1, 1, cookies=[{"name": "sid", "value": "old"}]
    )
    real_chmod = os.chmod

    def deny_temp_file(path, mode):
        if Path(path).name.startswith(".tmp_slot_"):
            raise PermissionError("simulated denial")
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", deny_temp_file)
    with pytest.raises(PermissionError):
        await store.save_session("slot_a", "http://proxy:8080", 2, 2, cookies=[])
    original = await store.load_session("slot_a")
    assert original is not None and original.cookies[0]["value"] == "old"
    assert not list(store.base_dir.glob(".tmp_slot_*"))


@pytest.mark.asyncio
async def test_cleanup_failure_is_reported_without_secret_values(tmp_path, monkeypatch, caplog):
    store = SessionStore(tmp_path / "sessions")

    def deny_replace(source, destination):
        raise OSError("replace fault")

    def deny_unlink(path, *args, **kwargs):
        raise OSError("cleanup fault")

    monkeypatch.setattr(os, "replace", deny_replace)
    monkeypatch.setattr(Path, "unlink", deny_unlink)
    with pytest.raises(OSError, match="replace fault"):
        await store.save_session(
            "slot_a", "http://proxy:8080", 1, 1, cookies=[{"name": "sid", "value": "private"}]
        )
    assert "Session temporary file cleanup failed" in caplog.text
    assert "private" not in caplog.text
    leftovers = list(store.base_dir.glob(".tmp_slot_*"))
    assert len(leftovers) == 1 and stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600
