"""Task-local proof that a native operation passed capability admission.

Only the trusted dispatcher binds this context. It is not a model argument,
permission grant or persisted identity; domain actor checks still apply.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_EXECUTING_CAPABILITY: ContextVar[str] = ContextVar(
    "life_engine_executing_capability",
    default="",
)


def current_capability_execution() -> str:
    """Return the admitted native capability for this task, if any."""
    return _EXECUTING_CAPABILITY.get()


@contextmanager
def bind_capability_execution(capability_id: str) -> Iterator[None]:
    """Bind and reset even when execution fails or is cancelled."""
    token = _EXECUTING_CAPABILITY.set(capability_id)
    try:
        yield
    finally:
        _EXECUTING_CAPABILITY.reset(token)
