"""Durable command contracts for the phase-three application API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class CommandStatus(StrEnum):
    """Public command lifecycle states."""

    ACCEPTED = "accepted"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATUSES = frozenset(
    {
        CommandStatus.SUCCEEDED,
        CommandStatus.FAILED,
        CommandStatus.DELIVERY_UNKNOWN,
        CommandStatus.REJECTED,
        CommandStatus.CANCELLED,
        CommandStatus.EXPIRED,
    }
)


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """Safe in-process projection of one durable command."""

    command_id: str
    idempotency_key: str
    request_hash: str
    command_type: str
    schema_version: int
    actor_id: str
    caller_role: str
    scope_snapshot: tuple[str, ...]
    target: dict[str, Any]
    payload: dict[str, Any]
    status: CommandStatus
    created_at: datetime
    accepted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result_event_id: str | None
    result: dict[str, Any] | None
    error_code: str | None
    safe_error_detail: str | None
    correlation_id: str | None
    causation_id: str | None
    expected_revision: int | None
    attempt_count: int
    cancellation_requested: bool
    task_id: str | None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """A handler result with an explicit delivery certainty."""

    status: CommandStatus
    result: dict[str, Any] | None = None
    error_code: str | None = None
    safe_error_detail: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            CommandStatus.SUCCEEDED,
            CommandStatus.FAILED,
            CommandStatus.DELIVERY_UNKNOWN,
            CommandStatus.REJECTED,
        }:
            raise ValueError("handler outcome must be terminal")


class IdempotencyConflict(RuntimeError):
    """The actor reused an idempotency key with different content."""


class CommandNotFound(KeyError):
    """The command is absent or intentionally hidden from the caller."""


class CommandNotCancellable(RuntimeError):
    """The command is terminal or its handler does not support cancellation."""


__all__ = [
    "TERMINAL_STATUSES",
    "CommandNotCancellable",
    "CommandNotFound",
    "CommandOutcome",
    "CommandRecord",
    "CommandStatus",
    "IdempotencyConflict",
]
