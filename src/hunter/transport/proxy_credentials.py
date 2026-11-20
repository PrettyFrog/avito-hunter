"""Private, endpoint-bound per-slot proxy credentials; no network operations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from hunter.domain.slots import validate_slot_id


class ProxyCredentialError(Exception):
    """Secret missing, unsafe, invalid, or bound to a different endpoint."""


@dataclass(frozen=True, slots=True)
class ProxyCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


def _endpoint_hash(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        _ = parsed.port
    except ValueError as exc:
        raise ProxyCredentialError("Invalid proxy endpoint") from exc
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


class ProxyCredentialStore:
    """Store secrets in owner-only files bound to the slot and proxy endpoint."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ProxyCredentialError("Credential directory is a symlink")
        os.chmod(self.directory, 0o700)
        info = self.directory.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ProxyCredentialError("Credential directory is not private")

    def path_for(self, slot_id: str) -> Path:
        validate_slot_id(slot_id)
        return self.directory / f"slot_{slot_id}_proxy.json"

    def import_fd(self, slot_id: str, endpoint: str, fd: int) -> None:
        path = self.path_for(slot_id)
        binding = _endpoint_hash(endpoint)
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ProxyCredentialError("Credential input is too large")
        try:
            data = json.loads(raw)
            username, password = data["username"], data["password"]
            if not all(isinstance(value, str) and value for value in (username, password)):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            raise ProxyCredentialError("Invalid credential input") from exc
        serialized = json.dumps(
            {
                "slot_id": slot_id,
                "endpoint_hash": binding,
                "username": username,
                "password": password,
            }
        )
        if len(serialized.encode("utf-8")) > 65536:
            raise ProxyCredentialError("Credential file would be too large")
        self._write(path, serialized)

    def _write(self, path: Path, data: str) -> None:
        file = NamedTemporaryFile(mode="w", dir=self.directory, prefix=".proxy_", delete=False)
        temporary = Path(file.name)
        try:
            os.fchmod(file.fileno(), 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(temporary, path)
        except BaseException:
            try:
                file.close()
                temporary.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning(
                    "Proxy credential temporary file cleanup failed"
                )
            raise

    def load(self, slot_id: str, endpoint: str) -> ProxyCredentials:
        path = self.path_for(slot_id)
        binding = _endpoint_hash(endpoint)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as file:
                info = os.fstat(file.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise ProxyCredentialError("Credential file is not private")
                raw = file.read(65537)
            if len(raw) > 65536:
                raise ProxyCredentialError("Credential file is too large")
            data = json.loads(raw)
            if data["slot_id"] != slot_id or data["endpoint_hash"] != binding:
                raise ProxyCredentialError("Credential binding mismatch")
            username, password = data["username"], data["password"]
            if not all(isinstance(value, str) and value for value in (username, password)):
                raise ProxyCredentialError("Invalid credential file")
            return ProxyCredentials(username, password)
        except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            raise ProxyCredentialError("Cannot read proxy credential file") from exc
