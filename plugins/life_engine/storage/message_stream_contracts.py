"""Contracts for immutable inbound messages and ordered stream turns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MessageConflict(RuntimeError):
    """An immutable message identity was reused with different content."""


class TurnClaimLost(RuntimeError):
    """A stale stream-turn owner attempted a fenced transition."""


class TurnStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    FAILED = "failed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    message_id: str
    platform: str
    platform_event_id: str
    occurrence_id: str
    payload_sha256: str
    stream_id: str
    reply_target: str
    source: str
    occurred_at: str
    received_at: str
    raw_payload_ref: str


@dataclass(frozen=True, slots=True)
class StreamTurn:
    turn_id: str
    stream_id: str
    stream_sequence: int
    source_message_id: str
    status: TurnStatus
    claim_owner: str | None
    claim_epoch: int
    lease_until: str | None
    input_frontier: dict[str, Any]
    result_ref: str | None
    result_digest: str | None
    attempts: int
    created_at: str
    updated_at: str


__all__ = ["InboundMessage", "MessageConflict", "StreamTurn", "TurnClaimLost", "TurnStatus"]
