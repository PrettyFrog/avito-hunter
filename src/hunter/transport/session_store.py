"""Versioned cookie and session storage with atomic replacement (S5 / T-478).

Contracts:
- Atomic replacement: writes to temporary file on same filesystem, fsyncs, then renames.
- Bounded secret file per slot with restrictive permissions (0o600 file, 0o700 dir).
- Endpoint/IP identity binding: rejects sessions if bound endpoint differs.
- Version compare: rejects stale version or stale generation updates.
- Secrets off logs: repr/str redact cookie values to prevent credential leaks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from hunter.domain.slots import validate_slot_id


class SessionStoreError(Exception):
    """Base exception for session store operations."""


class StaleSessionVersionError(SessionStoreError):
    """Raised when an update is attempted with stale version or generation."""


class EndpointMismatchError(SessionStoreError):
    """Raised when a session's bound endpoint/IP does not match expected endpoint."""


class CorruptSessionFileError(SessionStoreError):
    """Raised when session file exists but contains invalid data."""


class InvalidSessionImportError(SessionStoreError):
    """Raised when a protected descriptor does not contain an Avito session."""


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    slot_id: str
    endpoint: str = field(repr=False)
    version: int
    generation: int
    cookie_hash: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "endpoint": self.endpoint,
            "version": self.version,
            "generation": self.generation,
            "cookie_hash": self.cookie_hash,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class SessionPayload:
    metadata: SessionMetadata
    cookies: list[dict[str, Any]] = field(default_factory=list)
    storage_state: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        cookie_names = [c.get("name", "") for c in self.cookies]
        return (
            f"SessionPayload(slot_id={self.metadata.slot_id!r}, "
            f"version={self.metadata.version}, generation={self.metadata.generation}, "
            f"cookies_count={len(self.cookies)}, cookie_names={cookie_names!r}, "
            f"cookie_hash={self.metadata.cookie_hash!r})"
        )

    def __str__(self) -> str:
        return repr(self)

    def redacted_dict(self) -> dict[str, Any]:
        redacted_cookies = [
            {k: ("[REDACTED]" if k in ("value", "authToken") else v) for k, v in c.items()}
            for c in self.cookies
        ]
        metadata = self.metadata.to_dict()
        metadata["endpoint"] = "[REDACTED]"
        return {
            "metadata": metadata,
            "cookies": redacted_cookies,
            "has_storage_state": bool(self.storage_state),
        }


def compute_cookie_hash(cookies: list[dict[str, Any]]) -> str:
    """Computes a deterministic SHA-256 fingerprint for a list of cookies."""
    norm = []
    for c in sorted(
        cookies, key=lambda x: (x.get("domain", ""), x.get("path", ""), x.get("name", ""))
    ):
        norm.append(
            f"{c.get('domain', '')}|{c.get('path', '')}|{c.get('name', '')}|{c.get('value', '')}"
        )
    return hashlib.sha256(";".join(norm).encode("utf-8")).hexdigest()


class SessionStore:
    """Manages versioned, endpoint-bound session cookie files with atomic replacement."""

    def __init__(
        self,
        base_dir: Path | str,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
    ) -> None:
        if file_mode < 0 or file_mode & ~0o600 or dir_mode < 0 or dir_mode & ~0o700:
            raise ValueError("Session modes must not grant access to group or other users")
        self.base_dir = Path(base_dir)
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self._reference_resolver: Any = None
        self._reference_backend: Any = None
        self._ensure_dir()

    def bind_reference_resolver(self, resolver: Any) -> None:
        """Bind an async reader of committed per-identity file references."""
        self._reference_resolver = resolver

    def bind_reference_backend(self, writer: Any) -> None:
        """Use the owned writer for committed reads, updates and deletion tombstones."""
        from hunter.storage.session_mutations import SessionReferenceBackend
        from hunter.transport.session_ownership import bind_session_owner
        bind_session_owner(self.base_dir, Path(writer._conn_mgr.config.db_path))
        self._reference_backend = SessionReferenceBackend(self, writer)
        self._reference_resolver = self._reference_backend.resolve

    def prepare_publication(self, writer: Any, slot_id: str, version: int, generation: int):
        """Stage this task's matching save; activation remains the caller's responsibility."""
        if self._reference_backend is None:
            self.bind_reference_backend(writer)
        if self._reference_backend.writer is not writer:
            raise ValueError("Session store is bound to another writer")
        return self._reference_backend.preparation.reserve(slot_id, version, generation)

    def _ensure_dir(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base_dir, self.dir_mode)

    def _slot_path(self, slot_id: str) -> Path:
        validate_slot_id(slot_id)
        return self.base_dir / f"slot_{slot_id}_session.json"

    def _compare_version(
        self, existing: SessionMetadata, new_version: int, new_generation: int
    ) -> None:
        if new_version < existing.version:
            raise StaleSessionVersionError(
                f"Stale session version: incoming {new_version} < existing {existing.version}"
            )
        if new_version == existing.version and new_generation <= existing.generation:
            raise StaleSessionVersionError(
                f"Stale generation: incoming ({new_version}, {new_generation}) <= "
                f"existing ({existing.version}, {existing.generation})"
            )

    def _write_atomic(self, dest_path: Path, raw_json: str, slot_id: str) -> None:
        self._ensure_dir()
        temp_file = NamedTemporaryFile(
            mode="w",
            dir=self.base_dir,
            prefix=f".tmp_slot_{slot_id}_",
            delete=False,
            encoding="utf-8",
        )
        temp_path = Path(temp_file.name)
        try:
            os.chmod(temp_path, self.file_mode)
            temp_file.write(raw_json)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_file.close()

            os.replace(temp_path, dest_path)
        except BaseException:
            try:
                temp_file.close()
            except OSError:
                logging.getLogger(__name__).warning("Session temporary file close failed")
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("Session temporary file cleanup failed")
            raise

    async def save_session(
        self,
        slot_id: str,
        endpoint: str,
        version: int,
        generation: int,
        cookies: list[dict[str, Any]] | None = None,
        storage_state: dict[str, Any] | None = None,
        force: bool = False,
    ) -> SessionMetadata:
        self._require_write_backend()
        dest_path = self._slot_path(slot_id)
        existing = await self.load_session(slot_id)
        if existing and not force:
            self._compare_version(existing.metadata, version, generation)

        cookie_list = list(cookies or [])
        if not cookie_list and storage_state and "cookies" in storage_state:
            cookie_list = list(storage_state.get("cookies", []))
        if self._reference_backend is not None:
            return await self._reference_backend.save(
                slot_id, endpoint, version, generation, cookie_list, storage_state or {}, force,
            )

        c_hash = compute_cookie_hash(cookie_list)
        now_ts = datetime.now(UTC).isoformat()
        metadata = SessionMetadata(
            slot_id=slot_id,
            endpoint=endpoint,
            version=version,
            generation=generation,
            cookie_hash=c_hash,
            updated_at=now_ts,
        )

        data = {
            "metadata": metadata.to_dict(),
            "cookies": cookie_list,
            "storage_state": storage_state or {},
        }
        raw_json = json.dumps(data, indent=2, ensure_ascii=False)
        self._write_atomic(dest_path, raw_json, slot_id)
        return metadata

    @staticmethod
    def _parse_import(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidSessionImportError("Invalid session import data") from exc
        if not isinstance(data, dict):
            raise InvalidSessionImportError("Invalid session import data")
        state = data.get("storage_state", data)
        if not isinstance(state, dict):
            raise InvalidSessionImportError("Invalid session import data")
        cookies = data.get("cookies", state.get("cookies"))
        if not isinstance(cookies, list) or not cookies or len(cookies) > 500:
            raise InvalidSessionImportError("Session import must contain 1..500 cookies")
        for cookie in cookies:
            if not isinstance(cookie, dict) or not all(
                isinstance(cookie.get(key), str) and cookie[key]
                for key in ("name", "value", "domain")
            ):
                raise InvalidSessionImportError("Invalid cookie in session import")
            domain = cookie["domain"].lower().lstrip(".")
            if domain != "avito.ru" and not domain.endswith(".avito.ru"):
                raise InvalidSessionImportError("Session cookies must belong to avito.ru")
        return list(cookies), state

    async def import_session_fd(
        self,
        *,
        fd: int,
        slot_id: str,
        endpoint: str,
        version: int,
        generation: int,
    ) -> SessionMetadata:
        """Read bounded Playwright storage state from an inherited protected fd."""
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise InvalidSessionImportError("Session import exceeds 1 MiB")
        cookies, state = self._parse_import(raw)
        try:
            existing = await self.load_session(slot_id)
        except Exception:
            existing = None
        next_version = max(version, existing.metadata.version + 1 if existing else version)
        return await self.save_session(
            slot_id=slot_id,
            endpoint=endpoint,
            version=next_version,
            generation=generation,
            cookies=cookies,
            storage_state=state,
        )

    async def load_session(
        self, slot_id: str, expected_endpoint: str | None = None
    ) -> SessionPayload | None:
        dest_path = self._slot_path(slot_id)
        reference = await self._reference_resolver(slot_id) if self._reference_resolver else None
        if self._reference_resolver is None:
            from hunter.transport.session_ownership import read_owned_reference
            reference = read_owned_reference(self.base_dir, slot_id)
        if reference is not None:
            version, filename = reference
            if filename is None:
                return None
            from hunter.storage.session_reference import validate_candidate_name
            validate_candidate_name(slot_id, filename)
            dest_path = self.base_dir / filename
            if dest_path.is_symlink() or not dest_path.is_file():
                raise CorruptSessionFileError("Committed session candidate is missing or unsafe")
        if not dest_path.exists():
            return None

        try:
            content = dest_path.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as exc:
            raise CorruptSessionFileError(f"Corrupt session file {dest_path}: {exc}") from exc

        meta_dict = data.get("metadata", {})
        if not isinstance(meta_dict, dict) or meta_dict.get("slot_id") != slot_id:
            raise CorruptSessionFileError("Session slot identity mismatch")
        if reference is not None and meta_dict.get("version") != version:
            raise CorruptSessionFileError("Session candidate version mismatch")
        metadata = SessionMetadata(
            slot_id=slot_id,
            endpoint=meta_dict.get("endpoint", ""),
            version=int(meta_dict.get("version", 1)),
            generation=int(meta_dict.get("generation", 1)),
            cookie_hash=meta_dict.get("cookie_hash", ""),
            updated_at=meta_dict.get("updated_at", ""),
        )

        if expected_endpoint is not None and metadata.endpoint != expected_endpoint:
            raise EndpointMismatchError(f"Endpoint mismatch for slot '{slot_id}'")

        return SessionPayload(
            metadata=metadata,
            cookies=list(data.get("cookies", [])),
            storage_state=dict(data.get("storage_state", {})),
        )

    def has_session(self, slot_id: str) -> bool:
        if self._reference_backend is not None:
            return self._reference_backend.has_session(slot_id)
        from hunter.transport.session_ownership import read_owned_reference
        reference = read_owned_reference(self.base_dir, slot_id)
        if reference is not None:
            candidate = self.base_dir / reference[1] if reference[1] is not None else None
            return candidate is not None and candidate.is_file() and not candidate.is_symlink()
        return self._slot_path(slot_id).exists()

    async def delete_session(self, slot_id: str) -> bool:
        self._require_write_backend()
        if self._reference_backend is not None:
            return await self._reference_backend.delete(slot_id)
        path = self._slot_path(slot_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def _require_write_backend(self) -> None:
        marker = self.base_dir / ".session_owner.json"
        managed = self._reference_resolver is not None or marker.exists() or marker.is_symlink()
        if managed and self._reference_backend is None:
            raise SessionStoreError("Managed profile mutations require the owned writer backend")

    async def delete_session_if_matches(self, slot_id: str, version: int, generation: int, *, slot_fence=None) -> bool:
        """Automatic cleanup requires owned writer CAS against the captured profile fence."""
        if self._reference_backend is None:
            return False
        return await self._reference_backend.delete(slot_id, expected_fence=(version, generation), slot_fence=slot_fence)

    async def next_save_fence(self, slot_id: str, *, only_absent: bool = False) -> tuple[int, int]:
        """Capture a proposal before HTTP; tombstones remain monotonic fences."""
        self._require_write_backend()
        if not only_absent:
            profile = await self.load_session(slot_id)
            if profile is not None:
                return profile.metadata.version, profile.metadata.generation + 1
        if self._reference_backend is not None:
            from hunter.storage.session_reference import read_reference_record
            record = await self._reference_backend.writer.submit(lambda c: read_reference_record(c, slot_id))
            if record is not None:
                if record["file"] is not None:
                    raise StaleSessionVersionError("Profile changed while capturing absent-state fence")
                return record["version"], record.get("generation", 0) + 1
        elif self._slot_path(slot_id).exists():
            raise StaleSessionVersionError("Legacy profile appeared while capturing absence")
        return 1, 1
