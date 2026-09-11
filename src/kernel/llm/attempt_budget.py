"""Task-local attempt deadlines inside a caller-owned model-turn budget."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _TurnBudget:
    deadline: float
    attempt_cap: float


_TURN_BUDGET: ContextVar[_TurnBudget | None] = ContextVar(
    "llm_model_turn_budget", default=None
)
_ATTEMPTS_STARTED: ContextVar[int] = ContextVar("llm_attempts_started", default=0)


@contextmanager
def model_turn_attempt_budget(timeout_seconds: float) -> Iterator[None]:
    """Reserve time for a second attempt and cleanup within one model turn."""
    seconds = float(timeout_seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("model turn budget must be finite and positive")
    usable = seconds - min(1.0, seconds * 0.1)
    deadline = time.monotonic() + usable
    cap = usable / 2.0
    parent = _TURN_BUDGET.get()
    if parent is not None:
        deadline = min(deadline, parent.deadline)
        cap = min(cap, parent.attempt_cap)
    token = _TURN_BUDGET.set(_TurnBudget(deadline=deadline, attempt_cap=cap))
    attempts_token = _ATTEMPTS_STARTED.set(0)
    try:
        yield
    finally:
        _ATTEMPTS_STARTED.reset(attempts_token)
        _TURN_BUDGET.reset(token)


def effective_attempt_timeout(configured_seconds: object, *, model_count: int) -> object:
    """Return a timeout bounded by the caller's remaining turn budget."""
    budget = _TURN_BUDGET.get()
    if budget is None:
        return configured_seconds
    attempts_started = _ATTEMPTS_STARTED.get()
    remaining = budget.deadline - time.monotonic()
    if remaining <= 0:
        # A previous provider attempt may have consumed the deadline while
        # failure bookkeeping was running. Keep a tiny, bounded handoff window
        # for an already-approved fallback; a fresh scope still fails closed.
        if attempts_started > 0:
            return 0.05
        raise TimeoutError("model turn budget exhausted before next attempt")
    ceiling = min(remaining, budget.attempt_cap) if model_count > 1 else remaining
    if (
        isinstance(configured_seconds, (int, float))
        and math.isfinite(configured_seconds)
        and configured_seconds > 0
    ):
        return min(float(configured_seconds), ceiling)
    return ceiling


def mark_attempt_started() -> None:
    """Record that a provider transport was actually started."""
    _ATTEMPTS_STARTED.set(_ATTEMPTS_STARTED.get() + 1)
