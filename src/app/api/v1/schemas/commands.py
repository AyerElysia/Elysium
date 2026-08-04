"""Public schemas for durable command submission and tracking."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from .common import TimestampedModel, VersionedModel

CommandStatusValue = Literal[
    "accepted",
    "executing",
    "succeeded",
    "failed",
    "delivery_unknown",
    "rejected",
    "cancelled",
    "expired",
]


class CommandCreateRequest(VersionedModel):
    """One explicit side-effect request; transport idempotency stays in a header."""

    command_type: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", max_length=160)
    target: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)

    @field_validator("target", "payload")
    @classmethod
    def require_json_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Copy mappings so handlers cannot mutate the validated request object."""

        return dict(value)


class CommandResponse(VersionedModel, TimestampedModel):
    """Safe command projection without credentials or hidden handler arguments."""

    command_id: str = Field(min_length=1, max_length=100)
    command_type: str = Field(min_length=1, max_length=160)
    actor_id: str = Field(min_length=1, max_length=200)
    status: CommandStatusValue
    target: dict[str, Any]
    created_at: datetime
    accepted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_event_id: str | None = Field(default=None, max_length=240)
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=120)
    safe_error_detail: str | None = Field(default=None, max_length=500)
    correlation_id: str | None = Field(default=None, max_length=160)
    attempt_count: int = Field(ge=0)
    cancellation_requested: bool = False


class CommandListResponse(VersionedModel):
    """Bounded command query result."""

    commands: tuple[CommandResponse, ...]
    count: int = Field(ge=0, le=200)


class CommandCancelResponse(VersionedModel):
    """Cancellation acceptance is distinct from terminal cancellation."""

    command: CommandResponse
    cancellation_requested: bool


__all__ = [
    "CommandCancelResponse",
    "CommandCreateRequest",
    "CommandListResponse",
    "CommandResponse",
    "CommandStatusValue",
]
