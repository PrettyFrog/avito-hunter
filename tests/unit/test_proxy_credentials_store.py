import json
import os
import stat

import pytest

from hunter.transport.proxy_credentials import ProxyCredentialError, ProxyCredentialStore


def _fd_for(tmp_path, payload):
    path = tmp_path / "input.json"
    path.write_bytes(payload)
    return os.open(path, os.O_RDONLY)


def test_import_keeps_credentials_private_and_bound(tmp_path):
    store = ProxyCredentialStore(tmp_path / "secrets")
    fd = _fd_for(tmp_path, b'{"username":"operator","password":"secret-value"}')
    try:
        store.import_fd("slot_1", "http://proxy.example:8080", fd)
    finally:
        os.close(fd)
    credentials = store.load("slot_1", "http://proxy.example:8080")
    assert (credentials.username, credentials.password) == ("operator", "secret-value")
    assert "secret-value" not in repr(credentials)
    assert stat.S_IMODE(store.path_for("slot_1").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    with pytest.raises(ProxyCredentialError):
        store.load("slot_1", "http://other.example:8080")


@pytest.mark.parametrize(
    "payload", [b"{}", b"not-json", b'{"username":"x","password":""}', b"x" * 65537]
)
def test_invalid_import_does_not_replace_existing_secret(tmp_path, payload):
    store = ProxyCredentialStore(tmp_path / "secrets")
    valid = _fd_for(tmp_path, json.dumps({"username": "u", "password": "p"}).encode())
    try:
        store.import_fd("slot", "http://proxy:80", valid)
    finally:
        os.close(valid)
    invalid = _fd_for(tmp_path, payload)
    try:
        with pytest.raises(ProxyCredentialError):
            store.import_fd("slot", "http://proxy:80", invalid)
    finally:
        os.close(invalid)
    assert store.load("slot", "http://proxy:80").password == "p"


def test_reject_insecure_or_symlink_secret_file(tmp_path):
    store = ProxyCredentialStore(tmp_path / "secrets")
    fd = _fd_for(tmp_path, b'{"username":"u","password":"p"}')
    try:
        store.import_fd("slot", "http://proxy:80", fd)
    finally:
        os.close(fd)
    path = store.path_for("slot")
    path.chmod(0o644)
    with pytest.raises(ProxyCredentialError):
        store.load("slot", "http://proxy:80")
    path.unlink()
    path.symlink_to(tmp_path / "input.json")
    with pytest.raises(ProxyCredentialError):
        store.load("slot", "http://proxy:80")


def test_failed_atomic_replace_preserves_previous_secret(tmp_path, monkeypatch):
    store = ProxyCredentialStore(tmp_path / "secrets")
    old_fd = _fd_for(tmp_path, b'{"username":"u","password":"old"}')
    try:
        store.import_fd("slot", "http://proxy:80", old_fd)
    finally:
        os.close(old_fd)
    new_fd = _fd_for(tmp_path, b'{"username":"u","password":"new"}')

    def fail_replace(*_args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    try:
        with pytest.raises(OSError, match="injected replace failure"):
            store.import_fd("slot", "http://proxy:80", new_fd)
    finally:
        os.close(new_fd)
    assert store.load("slot", "http://proxy:80").password == "old"
    assert not list(store.directory.glob(".proxy_*"))


def test_import_rejects_credentials_that_expand_beyond_file_limit(tmp_path):
    store = ProxyCredentialStore(tmp_path / "secrets")
    payload = json.dumps({"username": "u", "password": "я" * 15000}, ensure_ascii=False)
    fd = _fd_for(tmp_path, payload.encode())
    try:
        with pytest.raises(ProxyCredentialError, match="too large"):
            store.import_fd("slot", "http://proxy:80", fd)
    finally:
        os.close(fd)
    assert not store.path_for("slot").exists()
