"""Contracts for durable external actions in a shared generation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class OutboxStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SENDING = "sending"
    SENT = "sent"
    RETRYABLE = "retryable"
    FAILED = "failed"
    UNKNOWN = "unknown"


class OutboxConflict(RuntimeError):
    """One action identity was reused with different content."""


class OutboxClaimLost(RuntimeError):
    """A stale outbox owner attempted a fenced transition."""


class UnknownOutboxAction(RuntimeError):
    """An unknown provider result cannot be retried blindly."""


@dataclass(frozen=True, slots=True)
class OutboxAction:
    action_id: str
    idempotency_key: str
    source_event_id: str
    stream_id: str
    target: str
    payload_ref: str
    payload_sha256: str
    status: OutboxStatus
    claim_owner: str | None
    claim_epoch: int
    lease_until: str | None
    provider_request_id: str | None
    provider_receipt_id: str | None
    attempts: int
    last_error_type: str | None
    created_at: str
    updated_at: str


@runtime_checkable
class OutboxStorePort(Protocol):
    async def create_action(self, action: OutboxAction) -> OutboxAction: ...
    async def claim_action(self, action_id: str, *, owner_id: str, lease_seconds: int) -> OutboxAction | None: ...
    async def mark_sending(self, action_id: str, *, owner_id: str, claim_epoch: int, provider_request_id: str) -> OutboxAction: ...
    async def mark_sent(self, action_id: str, *, owner_id: str, claim_epoch: int, provider_receipt_id: str) -> OutboxAction: ...
    async def mark_retryable(self, action_id: str, *, owner_id: str, claim_epoch: int, error_type: str) -> OutboxAction: ...
    async def mark_unknown(self, action_id: str, *, owner_id: str, claim_epoch: int, error_type: str) -> OutboxAction: ...


__all__ = ["OutboxAction", "OutboxClaimLost", "OutboxConflict", "OutboxStatus", "OutboxStorePort", "UnknownOutboxAction"]
