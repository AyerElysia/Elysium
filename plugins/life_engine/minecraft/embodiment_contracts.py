"""Transport-neutral contracts for Minecraft embodiment.

The contracts deliberately keep goals, operations, facts, and conclusions as
open text or JSON objects.  They describe evidence without assigning feelings
or deciding what Elysia should want.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

JsonObject = dict[str, Any]


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(UTC).isoformat()


def _identifier(prefix: str) -> str:
    """Create a globally unique, human-readable protocol identifier."""

    return f"{prefix}_{uuid4().hex}"


def _require_text(value: str, field_name: str) -> None:
    """Reject empty protocol fields instead of inventing a fallback value."""

    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class EmbodiedIntent:
    """An intention authored by Elysia for one explicitly selected body."""

    text: str
    body_name: str
    context: Mapping[str, Any] = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: _identifier("intent"))
    revision: int = 1
    issued_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """Validate identity while preserving open-ended intent content."""

        _require_text(self.text, "text")
        _require_text(self.body_name, "body_name")
        _require_text(self.intent_id, "intent_id")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        object.__setattr__(self, "context", dict(self.context))

    def to_wire(self) -> JsonObject:
        """Serialize the intent for a bridge or an append-only trace."""

        return {
            "intent_id": self.intent_id,
            "revision": self.revision,
            "issued_at": self.issued_at,
            "body_name": self.body_name,
            "text": self.text,
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class WorldObservation:
    """A factual observation emitted by a game body."""

    instance_id: str
    sequence: int
    observed_at: str
    facts: Mapping[str, Any]
    source: str
    observation_id: str = field(default_factory=lambda: _identifier("observation"))
    received_at: str = field(default_factory=utc_now)
    frame_path: str | None = None

    def __post_init__(self) -> None:
        """Validate ordering fields and copy caller-owned mappings."""

        _require_text(self.instance_id, "instance_id")
        _require_text(self.observation_id, "observation_id")
        _require_text(self.source, "source")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        object.__setattr__(self, "facts", dict(self.facts))

    def age_seconds(self, now: datetime | None = None) -> float:
        """Return observation age without deciding whether it is acceptable."""

        observed = datetime.fromisoformat(self.observed_at)
        reference = now or datetime.now(UTC)
        return max(0.0, (reference - observed).total_seconds())

    def to_wire(self) -> JsonObject:
        """Serialize the complete observation without truncation."""

        return {
            "observation_id": self.observation_id,
            "instance_id": self.instance_id,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "source": self.source,
            "facts": dict(self.facts),
            "frame_path": self.frame_path,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> WorldObservation:
        """Parse one observation received from a trusted bridge session."""

        return cls(
            observation_id=str(payload.get("observation_id") or _identifier("observation")),
            instance_id=str(payload["instance_id"]),
            sequence=int(payload["sequence"]),
            observed_at=str(payload["observed_at"]),
            received_at=utc_now(),
            source=str(payload["source"]),
            facts=dict(payload.get("facts") or {}),
            frame_path=(str(payload["frame_path"]) if payload.get("frame_path") else None),
        )


@dataclass(frozen=True, slots=True)
class ActionCommand:
    """One open-vocabulary operation proposed for a specific intention."""

    intent_id: str
    intent_revision: int
    operation: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: _identifier("command"))
    issued_at: str = field(default_factory=utc_now)
    based_on_observation: str | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        """Validate correlation data and preserve arbitrary operation payloads."""

        _require_text(self.intent_id, "intent_id")
        _require_text(self.command_id, "command_id")
        _require_text(self.operation, "operation")
        if self.intent_revision < 1:
            raise ValueError("intent_revision must be positive")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when supplied")
        object.__setattr__(self, "parameters", dict(self.parameters))

    def to_wire(self) -> JsonObject:
        """Serialize the complete command for execution."""

        return {
            "command_id": self.command_id,
            "intent_id": self.intent_id,
            "intent_revision": self.intent_revision,
            "issued_at": self.issued_at,
            "operation": self.operation,
            "parameters": dict(self.parameters),
            "based_on_observation": self.based_on_observation,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """Observable execution facts returned by a body.

    ``accepted`` and ``completed`` report protocol state.  They do not claim
    that the intention itself succeeded; that conclusion belongs to Elysia or
    her chosen planner and must cite evidence identifiers.
    """

    command_id: str
    intent_id: str
    accepted: bool
    completed: bool
    interrupted: bool
    facts: Mapping[str, Any] = field(default_factory=dict)
    receipt_id: str = field(default_factory=lambda: _identifier("receipt"))
    recorded_at: str = field(default_factory=utc_now)
    error: str | None = None
    observation_sequence: int | None = None

    def __post_init__(self) -> None:
        """Validate receipt identity and copy factual payloads."""

        _require_text(self.command_id, "command_id")
        _require_text(self.intent_id, "intent_id")
        _require_text(self.receipt_id, "receipt_id")
        object.__setattr__(self, "facts", dict(self.facts))

    @property
    def terminal(self) -> bool:
        """Return whether the body has finished reporting this command."""

        return self.completed or self.interrupted or self.error is not None

    def to_wire(self) -> JsonObject:
        """Serialize all receipt evidence without interpreting it."""

        return {
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "intent_id": self.intent_id,
            "accepted": self.accepted,
            "completed": self.completed,
            "interrupted": self.interrupted,
            "recorded_at": self.recorded_at,
            "facts": dict(self.facts),
            "error": self.error,
            "observation_sequence": self.observation_sequence,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ActionReceipt:
        """Parse a bridge receipt while retaining its factual payload."""

        return cls(
            receipt_id=str(payload.get("receipt_id") or _identifier("receipt")),
            command_id=str(payload["command_id"]),
            intent_id=str(payload["intent_id"]),
            accepted=bool(payload.get("accepted")),
            completed=bool(payload.get("completed")),
            interrupted=bool(payload.get("interrupted")),
            recorded_at=str(payload.get("recorded_at") or utc_now()),
            facts=dict(payload.get("facts") or {}),
            error=(str(payload["error"]) if payload.get("error") else None),
            observation_sequence=(
                int(payload["observation_sequence"])
                if payload.get("observation_sequence") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class IntentConclusion:
    """A planner-authored conclusion tied to explicit evidence records."""

    statement: str
    evidence_ids: tuple[str, ...]
    authored_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """Require a statement and at least one evidence reference."""

        _require_text(self.statement, "statement")
        if not self.evidence_ids:
            raise ValueError("a conclusion must cite evidence")

    def to_wire(self) -> JsonObject:
        """Serialize the conclusion and its evidence references."""

        return {
            "statement": self.statement,
            "evidence_ids": list(self.evidence_ids),
            "authored_at": self.authored_at,
        }


@dataclass(frozen=True, slots=True)
class PlannerTurn:
    """One planner decision: either act once or conclude with evidence."""

    command: ActionCommand | None = None
    conclusion: IntentConclusion | None = None
    private_reasoning_reference: str | None = None

    def __post_init__(self) -> None:
        """Require exactly one externally observable decision."""

        if (self.command is None) == (self.conclusion is None):
            raise ValueError("planner turn requires exactly one command or conclusion")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Complete evidence gathered while pursuing one intention."""

    intent: EmbodiedIntent
    observations: tuple[WorldObservation, ...]
    receipts: tuple[ActionReceipt, ...]
    conclusion: IntentConclusion | None
    interrupted: bool = False
    error: str | None = None
