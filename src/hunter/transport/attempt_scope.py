"""Instance-owned task-local operation context for HTTP attempt observers."""
from collections.abc import Callable, Iterable
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4


class AttemptScope:
    def __init__(self) -> None:
        self._state: ContextVar[dict[str, str] | None] = ContextVar("http_attempt_scope", default=None)

    @property
    def parent_id(self) -> str | None:
        return (self._state.get() or {}).get("parent_id")

    @contextmanager
    def use(self, purpose: str, parent_id: str | None = None):
        if purpose not in {"poll", "warmup", "retry", "recovery", "redirect"}:
            raise ValueError("Unknown attempt purpose")
        parent = parent_id or self.parent_id or uuid4().hex
        if len(parent) != 32 or any(c not in "0123456789abcdef" for c in parent):
            raise ValueError("Invalid operation correlation ID")
        token = self._state.set({"purpose": purpose, "parent_id": parent})
        try:
            yield
        finally:
            self._state.reset(token)

    def capture_emitter(self, sink: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
        fields = dict(self._state.get() or {"purpose": "poll", "parent_id": uuid4().hex})
        def emit(event: dict[str, Any]) -> None:
            sink({**event, **fields})
        return emit


@contextmanager
def client_scopes(clients: Iterable[Any], purpose: str):
    scopes = [scope for client in clients if isinstance(scope := getattr(client, "attempt_scope", None), AttemptScope)]
    parent = next((scope.parent_id for scope in scopes if scope.parent_id), None) or uuid4().hex
    with ExitStack() as stack:
        for scope in scopes:
            stack.enter_context(scope.use(purpose, parent))
        yield


async def scoped_http_call(client: Any, purpose: str, method: str, *args: Any, **kwargs: Any) -> Any:
    with client_scopes([client], purpose):
        return await getattr(client, method)(*args, **kwargs)
