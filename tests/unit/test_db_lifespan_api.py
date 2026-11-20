import inspect

from hunter.core.db import lifespan_db


def test_lifespan_db_remains_async_context_manager_factory() -> None:
    assert not inspect.isasyncgenfunction(lifespan_db)
    manager = lifespan_db()
    assert hasattr(manager, "__aenter__")
    assert hasattr(manager, "__aexit__")
