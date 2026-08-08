"""Backend-neutral contracts for multi-writer runtime operations and deltas."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class OperationStatus(StrEnum):
    """Persisted technical operation states with explicit recovery semantics."""

    PENDING = "pending"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    FAILED = "failed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class OperationConflict(RuntimeError):
    """Raised when an operation identity or committed result conflicts."""


class OperationClaimLost(RuntimeError):
    """Raised when a stale owner attempts a fenced transition or commit."""


class RuntimeDeltaConflict(RuntimeError):
    """Raised when a typed delta cannot be safely applied."""


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    operation_type: str
    scope_key: str
    sequence: int
    status: OperationStatus
    claim_owner: str | None
    claim_epoch: int
    lease_until: str | None
    input_frontier: dict[str, Any]
    result_ref: str | None
    result_sha256: str | None
    attempts: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    operation_id: str
    commit_revision: int
    result_sha256: str
    committed_by: str
    committed_at: str


@dataclass(frozen=True, slots=True)
class RuntimeDelta:
    operation_id: str
    namespace: str
    state_key: str
    delta_type: str
    schema_version: int
    payload: dict[str, Any]
    actor: str
    source: str
    causation_id: str
    created_at: str


@runtime_checkable
class OperationStorePort(Protocol):
    async def register_operation(
        self,
        *,
        operation_id: str,
        operation_type: str,
        scope_key: str,
        sequence: int,
        input_frontier: dict[str, Any] | None = None,
    ) -> OperationRecord:
        """Create an immutable operation identity or return its identical record."""

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        lease_seconds: int,
    ) -> OperationRecord | None:
        """Atomically claim pending/retryable/expired work."""

    async def commit_runtime_delta(
        self,
        delta: RuntimeDelta,
        *,
        owner_id: str,
        claim_epoch: int,
        result_ref: str,
        result_sha256: str,
    ) -> OperationReceipt:
        """Apply one typed delta and receipt in a short fenced transaction."""


__all__ = [
    "OperationClaimLost",
    "OperationConflict",
    "OperationReceipt",
    "OperationRecord",
    "OperationStatus",
    "OperationStorePort",
    "RuntimeDelta",
    "RuntimeDeltaConflict",
]
