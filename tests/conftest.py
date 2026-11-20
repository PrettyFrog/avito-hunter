from __future__ import annotations

import asyncio
import gc
import os
from pathlib import Path
import aiosqlite
import pytest
from hunter.core.config import Settings
from tests.fixtures.arch_state import FixedClock
from tests.fixtures.data_sandbox import create_test_data_sandbox


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    sandbox = create_test_data_sandbox(Path(__file__).resolve().parents[1])
    config._hunter_test_sandbox = sandbox
    config._hunter_test_previous_data_dir = os.environ.get("HUNTER_DATA_DIR")
    os.environ["HUNTER_DATA_DIR"] = sandbox.name


def pytest_unconfigure(config: pytest.Config) -> None:
    sandbox = getattr(config, "_hunter_test_sandbox", None)
    if sandbox is None:
        return
    previous = config._hunter_test_previous_data_dir
    if previous is None:
        os.environ.pop("HUNTER_DATA_DIR", None)
    else:
        os.environ["HUNTER_DATA_DIR"] = previous
    sandbox.cleanup()


@pytest.fixture(autouse=True)
def default_daytime_politeness(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if "test_time_politeness" in request.node.nodeid:
        return
    from hunter.collect.politeness import Politeness

    monkeypatch.setattr(Politeness, "is_night", lambda self: False)


@pytest.fixture(autouse=True)
def default_notify_mode(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if "test_pr0b2_recall" in request.node.nodeid:
        yield
        return
    from hunter.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "notify_mode", "new")
    yield
    get_settings.cache_clear()


@pytest.fixture
def sandbox_clock() -> FixedClock:
    from tests.fixtures.arch_state import FixedClock

    return FixedClock()


@pytest.fixture
def sandbox_settings(tmp_path: Path) -> Settings:
    from tests.fixtures.arch_state import create_sandbox_settings

    return create_sandbox_settings(tmp_path)


@pytest.fixture(autouse=True)
def block_external_sockets(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Enforce network prohibition in tests (S0 hermeticity)."""
    if request.node.get_closest_marker("allow_network"):
        return
    import socket

    orig_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) and len(address) > 0 else str(address)
        if host in ("127.0.0.1", "localhost", "::1") or str(host).startswith("/"):
            return orig_connect(self, address)
        raise RuntimeError(
            f"Hermetic sandbox violation: blocked outbound network connection to {address}"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
async def cleanup_aiosqlite():
    yield
    for obj in gc.get_objects():
        if isinstance(obj, aiosqlite.Connection) and getattr(obj, "_connection", None) is not None:
            try:
                await asyncio.wait_for(obj.close(), timeout=0.5)
            except Exception:
                # If close timed out or failed, forcibly stop worker thread
                try:
                    if hasattr(obj, "_running"):
                        obj._running = False
                    if hasattr(obj, "_connection") and obj._connection is not None:
                        obj._connection.close()
                        obj._connection = None
                    if (
                        hasattr(obj, "_thread")
                        and obj._thread is not None
                        and obj._thread.is_alive()
                    ):
                        # Worker thread is waiting on tx.get()
                        if hasattr(obj, "_tx"):
                            obj._tx.put_nowait(
                                (None, lambda: aiosqlite.core._STOP_RUNNING_SENTINEL)
                            )
                except Exception:
                    pass
