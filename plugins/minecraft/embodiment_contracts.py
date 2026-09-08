"""Transport-neutral contracts for Minecraft embodiment.

The contracts deliberately keep goals, operations, facts, and conclusions as
open text or JSON objects.  They describe evidence without assigning feelings
or deciding what Elysia should want.
"""

from __future__ import annotations

import hashlib
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
class PerceptionReference:
    """Content-free identity for one transient Perception delivery."""

    instance_id: str
    projection_kind: str
    from_position: int
    through_position: int
    frontier: int
    cursor_revision: int
    content_sha256: str
    content_bytes: int
    assertion_ids: tuple[str, ...]
    change_positions: tuple[int, ...]
    delivery_id: str
    version: str

    @classmethod
    def from_prepared(cls, prepared: Any) -> PerceptionReference:
        """Build a stable reference without retaining transient prompt content."""

        required = (
            "instance_id",
            "projection_kind",
            "from_position",
            "through_position",
            "source_frontier",
            "cursor_revision",
            "content",
            "assertion_ids",
            "change_positions",
            "delivery_id",
            "projection_sha256",
            "algorithm_version",
            "delivered_bytes",
        )
        missing = [name for name in required if not hasattr(prepared, name)]
        if missing:
            raise ValueError(
                "prepared perception is missing reference fields: "
                + ", ".join(missing)
            )
        instance_id = str(prepared.instance_id)
        _require_text(instance_id, "perception instance_id")
        content = prepared.content
        if not isinstance(content, str):
            raise TypeError("prepared perception content must be text")
        _require_text(content, "prepared perception content")
        from_position = int(prepared.from_position)
        through_position = int(prepared.through_position)
        cursor_revision = int(prepared.cursor_revision)
        if min(from_position, through_position, cursor_revision) < 0:
            raise ValueError("perception positions and revision must not be negative")
        if through_position < from_position:
            raise ValueError("perception through_position precedes from_position")
        assertion_ids = tuple(str(item) for item in prepared.assertion_ids)
        change_positions = tuple(int(item) for item in prepared.change_positions)
        if any(not item.strip() for item in assertion_ids):
            raise ValueError("perception assertion_ids must not contain empty values")
        content_bytes = content.encode("utf-8")
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        supplied_hash = str(prepared.projection_sha256 or "")
        if supplied_hash != content_sha256:
            raise ValueError("prepared perception hash does not match its content")
        supplied_bytes = int(prepared.delivered_bytes)
        if supplied_bytes != len(content_bytes):
            raise ValueError("prepared perception byte count does not match its content")
        projection_kind = str(prepared.projection_kind)
        _require_text(projection_kind, "perception projection_kind")
        frontier = int(prepared.source_frontier)
        if frontier < through_position:
            raise ValueError("perception frontier precedes through_position")
        version = str(prepared.algorithm_version)
        _require_text(version, "perception algorithm_version")
        delivery_id = str(prepared.delivery_id or "")
        _require_text(delivery_id, "perception delivery_id")
        return cls(
            instance_id=instance_id,
            projection_kind=projection_kind,
            from_position=from_position,
            through_position=through_position,
            frontier=frontier,
            cursor_revision=cursor_revision,
            content_sha256=content_sha256,
            content_bytes=len(content_bytes),
            assertion_ids=assertion_ids,
            change_positions=change_positions,
            delivery_id=delivery_id,
            version=version,
        )

    def to_wire(self) -> JsonObject:
        """Serialize provenance and delivery identity without prompt text."""

        return {
            "schema": "minecraft.perception_reference.v1",
            "delivery_id": self.delivery_id,
            "hash": self.content_sha256,
            "version": self.version,
            "instance_id": self.instance_id,
            "projection_kind": self.projection_kind,
            "from": self.from_position,
            "through": self.through_position,
            "frontier": self.frontier,
            "cursor_revision": self.cursor_revision,
            "source_ids": {
                "assertions": list(self.assertion_ids),
                "changes": list(self.change_positions),
            },
            "bytes": self.content_bytes,
        }


@dataclass(frozen=True, slots=True)
class EmbodiedIntent:
    """An intention authored by Elysia for one explicitly selected body."""

    text: str
    body_name: str
    durable_context: Mapping[str, Any] = field(default_factory=dict)
    transient_prompt_context: Mapping[str, Any] = field(default_factory=dict)
    perception_reference: PerceptionReference | None = None
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
        durable_context = dict(self.durable_context)
        transient_prompt_context = dict(self.transient_prompt_context)
        overlapping = set(durable_context).intersection(transient_prompt_context)
        if overlapping:
            raise ValueError(
                "durable and transient intent contexts overlap: "
                + ", ".join(sorted(overlapping))
            )
        object.__setattr__(self, "durable_context", durable_context)
        object.__setattr__(self, "transient_prompt_context", transient_prompt_context)

    @property
    def context(self) -> Mapping[str, Any]:
        """Expose a merged prompt-only compatibility view without persistence."""

        return {**self.durable_context, **self.transient_prompt_context}

    def to_prompt(self) -> JsonObject:
        """Serialize both durable and transient context for one planner call."""

        value = self.to_wire()
        value["transient_prompt_context"] = dict(self.transient_prompt_context)
        return value

    def to_wire(self) -> JsonObject:
        """Serialize durable intent state without transient prompt projections."""

        return {
            "intent_id": self.intent_id,
            "revision": self.revision,
            "issued_at": self.issued_at,
            "body_name": self.body_name,
            "text": self.text,
            "durable_context": dict(self.durable_context),
            "perception_reference": (
                self.perception_reference.to_wire()
                if self.perception_reference is not None
                else None
            ),
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
class MinecraftBodyEvent:
    """One ordered factual event emitted by an authenticated game body."""

    event_id: str
    instance_id: str
    sequence: int
    occurred_at: str
    source: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    received_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """Reject identity-free or non-namespaced events without interpretation."""

        for value, name in (
            (self.event_id, "event_id"),
            (self.instance_id, "instance_id"),
            (self.occurred_at, "occurred_at"),
            (self.source, "source"),
            (self.kind, "kind"),
        ):
            _require_text(value, name)
        if self.sequence < 1:
            raise ValueError("Minecraft body event sequence must be positive")
        if not self.kind.startswith("minecraft."):
            raise ValueError("Minecraft body event kind must use its namespace")
        object.__setattr__(self, "payload", dict(self.payload))

    def to_wire(self) -> JsonObject:
        """Serialize the complete bounded body event."""

        return {
            "event_id": self.event_id,
            "instance_id": self.instance_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "received_at": self.received_at,
            "source": self.source,
            "kind": self.kind,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> MinecraftBodyEvent:
        """Parse one event without inventing a replay identity."""

        return cls(
            event_id=str(payload["event_id"]),
            instance_id=str(payload["instance_id"]),
            sequence=int(payload["sequence"]),
            occurred_at=str(payload["occurred_at"]),
            received_at=utc_now(),
            source=str(payload["source"]),
            kind=str(payload["kind"]),
            payload=dict(payload.get("payload") or {}),
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
