"""Stable contracts shared by the local and remote sync ledgers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize a JSON value deterministically for hashing and comparison."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SyncStatus(StrEnum):
    HELD = "held"
    PENDING = "pending"
    INFLIGHT = "inflight"
    RETRY = "retry"
    CONFIRMED = "confirmed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SyncEnvelope:
    """Immutable event envelope transported across nodes."""

    event_id: str
    origin_node_id: str
    origin_sequence: int
    occurred_at: str
    recorded_at: str
    event_type: str
    actor_id: str
    consciousness_instance_id: str
    visibility: str
    causation_id: str
    correlation_id: str
    payload_json: str
    payload_hash: str
    schema_version: int = 1

    @classmethod
    def build(
        cls,
        *,
        event_id: str,
        origin_node_id: str,
        origin_sequence: int,
        occurred_at: str,
        recorded_at: str,
        event_type: str,
        payload: Any,
        actor_id: str = "",
        consciousness_instance_id: str = "",
        visibility: str = "private",
        causation_id: str = "",
        correlation_id: str = "",
        schema_version: int = 1,
    ) -> SyncEnvelope:
        payload_json = canonical_json(payload)
        return cls(
            event_id=str(event_id),
            origin_node_id=str(origin_node_id),
            origin_sequence=int(origin_sequence),
            occurred_at=str(occurred_at),
            recorded_at=str(recorded_at),
            event_type=str(event_type),
            actor_id=str(actor_id),
            consciousness_instance_id=str(consciousness_instance_id),
            visibility=str(visibility or "private").lower(),
            causation_id=str(causation_id),
            correlation_id=str(correlation_id),
            payload_json=payload_json,
            payload_hash=sha256_text(payload_json),
            schema_version=int(schema_version),
        )

    def payload(self) -> Any:
        return json.loads(self.payload_json)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Remote outcome.  Duplicate is success; conflict is not."""

    status: str
    remote_position: int = 0
    conflict_reason: str = ""
    existing_hash: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in {"accepted", "duplicate"}


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    envelope: SyncEnvelope
    lease_token: str
    attempt_count: int
