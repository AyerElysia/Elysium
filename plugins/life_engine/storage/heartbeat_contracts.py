"""Contracts for claimable, ordered heartbeat operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class HeartbeatStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    FAILED = "failed"
    CONFLICT = "conflict"


class HeartbeatClaimLost(RuntimeError):
    """A stale heartbeat owner attempted to commit."""


class HeartbeatConflict(RuntimeError):
    """Heartbeat identity, frontier, or result conflict."""


@dataclass(frozen=True, slots=True)
class HeartbeatOperation:
    heartbeat_operation_id: str
    consciousness_instance_id: str
    sequence: int
    input_frontier: dict[str, Any]
    prepared_context_digest: str | None
    status: HeartbeatStatus
    claim_owner: str | None
    claim_epoch: int
    lease_until: str | None
    model_request_id: str | None
    result_ref: str | None
    result_digest: str | None
    committed_frontier: int | None
    attempts: int
    created_at: str
    updated_at: str


__all__ = ["HeartbeatClaimLost", "HeartbeatConflict", "HeartbeatOperation", "HeartbeatStatus"]
