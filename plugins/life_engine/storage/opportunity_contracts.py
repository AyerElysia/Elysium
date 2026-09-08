"""Canonical storage contracts for the subject-managed opportunity plane.

Only engineering state is modelled here.  Availability and exact perception
must never be interpreted as importance, acceptance, rejection or completion.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from src.kernel.storage import canonical_json

OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE = "life_engine.opportunity"
OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY = "scheduler"
OPPORTUNITY_MANAGED_MARKER_KEY = "canonical_managed_v1"

_MAX_ID = 255
_MAX_REASON_BYTES = 1024 * 1024
_MAX_WORKFLOW_BYTES = 8 * 1024 * 1024
_MAX_REFS = 256
_MAX_LIMIT = 1000
_MAX_INTERVAL_SECONDS = 366 * 24 * 60 * 60


class ProviderStatus(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"
    UNINSTALLED = "uninstalled"


class ProviderAction(StrEnum):
    INSTALL = "install"
    BIND_WORKFLOW = "bind_workflow"
    PAUSE = "pause"
    RESUME = "resume"
    UNINSTALL = "uninstall"


class OpportunityStatus(StrEnum):
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"


class OpportunityAction(StrEnum):
    OPEN = "open"
    CONFIGURE = "configure"
    PAUSE = "pause"
    RESUME = "resume"
    CLOSE = "close"


class OpportunityOrigin(StrEnum):
    SUBJECT = "subject"
    PROVIDER = "provider"


class OpportunitySchedule(StrEnum):
    MANUAL = "manual"
    AT = "at"
    INTERVAL = "interval"


class PublicationStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class OpportunityHistoryFamily(StrEnum):
    PROVIDER = "provider"
    WORKFLOW = "workflow"
    REGISTRATION = "registration"


class OpportunityConflict(RuntimeError):
    """Stable occurrence reuse or revision CAS conflict."""

    def __init__(
        self,
        message: str = "OpportunityConflict",
        *,
        scope: str = "",
        identity: str = "",
        expected_revision: int | None = None,
        actual_revision: int | None = None,
    ) -> None:
        self.scope = scope
        self.identity = identity
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        if scope or identity:
            message = (
                "OpportunityConflict:"
                f"scope={scope}:identity={identity}:"
                f"expected_revision={expected_revision}:"
                f"actual_revision={actual_revision}"
            )
        super().__init__(message)


class OpportunityActorInactive(RuntimeError):
    """A subject command actor is not an active consciousness instance."""


class OpportunityTransitionError(RuntimeError):
    """A requested technical lifecycle transition is invalid."""


class OpportunitySchedulerClaimRequired(RuntimeError):
    """Scheduler state was accessed without the injected singleton claim."""


class OpportunityDeliveryRejected(RuntimeError):
    """A final-attempt exact perception receipt could not be proven."""


@dataclass(frozen=True, slots=True)
class OpportunityRuntimeMarker:
    """Irreversible infrastructure fact that canonical management began.

    This is not a subject decision and does not install any provider or
    workflow.  Its only purpose is preventing legacy hard-coded automation
    from silently reviving after the canonical opportunity plane was adopted.
    """

    generation_id: str
    migration_occurrence_id: str
    schema_version: int
    activated_at: str
    marker_key: str = OPPORTUNITY_MANAGED_MARKER_KEY
    marker_sha256: str = ""

    def __post_init__(self) -> None:
        generation_id = _text(self.generation_id, "generation_id")
        occurrence_id = _text(
            self.migration_occurrence_id,
            "migration_occurrence_id",
        )
        schema_version = _revision(
            self.schema_version,
            "schema_version",
            positive=True,
        )
        activated_at = _timestamp(self.activated_at, "activated_at")
        if self.marker_key != OPPORTUNITY_MANAGED_MARKER_KEY:
            raise ValueError("marker_key is not the canonical managed marker")
        expected = _hash(
            {
                "marker_key": OPPORTUNITY_MANAGED_MARKER_KEY,
                "generation_id": generation_id,
                "migration_occurrence_id": occurrence_id,
                "schema_version": schema_version,
                "activated_at": activated_at,
            }
        )
        if self.marker_sha256:
            digest = _digest(self.marker_sha256, "marker_sha256")
            if digest != expected:
                raise ValueError("marker_sha256 does not match marker fields")
        else:
            digest = expected
        object.__setattr__(self, "generation_id", generation_id)
        object.__setattr__(self, "migration_occurrence_id", occurrence_id)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "activated_at", activated_at)
        object.__setattr__(self, "marker_sha256", digest)


def _text(value: object, field: str, maximum: int = _MAX_ID) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{field} must be 1..{maximum} characters")
    return result


def _optional_text(value: object, field: str, maximum: int = _MAX_ID) -> str:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def _digest(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return result


def _timestamp(value: object, field: str, *, allow_empty: bool = False) -> str:
    result = str(value or "").strip()
    if allow_empty and not result:
        return ""
    try:
        parsed = datetime.fromisoformat(result)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.isoformat()


def _revision(value: object, field: str, *, positive: bool = False) -> int:
    result = int(value)
    if result < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must be {qualifier}")
    return result


def _refs(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    result = tuple(_text(item, field) for item in values)
    if len(result) > _MAX_REFS or len(set(result)) != len(result):
        raise ValueError(f"{field} must contain at most {_MAX_REFS} unique ids")
    return result


def _reason(value: object) -> str:
    result = str(value or "")
    if len(result.encode("utf-8")) > _MAX_REASON_BYTES:
        raise ValueError("reason exceeds its storage byte limit")
    return result


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _limit(value: int) -> int:
    result = int(value)
    if result <= 0 or result > _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    return result


@dataclass(frozen=True, slots=True)
class ProviderBindingCommand:
    """One explicit subject decision about a provider lifecycle."""

    occurrence_id: str
    provider_id: str
    action: ProviderAction
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    expected_revision: int
    descriptor_version: str
    descriptor_sha256: str
    workflow_id: str
    workflow_revision: int
    workflow_sha256: str
    reason: str
    occurred_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "occurrence_id", _text(self.occurrence_id, "occurrence_id")
        )
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        action = ProviderAction(self.action)
        object.__setattr__(self, "action", action)
        object.__setattr__(
            self,
            "actor_consciousness_instance_id",
            _text(
                self.actor_consciousness_instance_id, "actor_consciousness_instance_id"
            ),
        )
        object.__setattr__(
            self,
            "source_instance_id",
            _text(self.source_instance_id, "source_instance_id"),
        )
        object.__setattr__(
            self,
            "source_occurrence_ids",
            _refs(self.source_occurrence_ids, "source_occurrence_ids"),
        )
        object.__setattr__(
            self,
            "causation_occurrence_id",
            _text(self.causation_occurrence_id, "causation_occurrence_id"),
        )
        expected = _revision(self.expected_revision, "expected_revision")
        if action != ProviderAction.INSTALL and expected == 0:
            raise ValueError("existing provider actions require expected_revision > 0")
        object.__setattr__(self, "expected_revision", expected)
        object.__setattr__(
            self,
            "descriptor_version",
            _text(self.descriptor_version, "descriptor_version", 128),
        )
        object.__setattr__(
            self,
            "descriptor_sha256",
            _digest(self.descriptor_sha256, "descriptor_sha256"),
        )
        object.__setattr__(self, "workflow_id", _text(self.workflow_id, "workflow_id"))
        object.__setattr__(
            self,
            "workflow_revision",
            _revision(self.workflow_revision, "workflow_revision", positive=True),
        )
        object.__setattr__(
            self, "workflow_sha256", _digest(self.workflow_sha256, "workflow_sha256")
        )
        object.__setattr__(self, "reason", _reason(self.reason))
        object.__setattr__(
            self, "occurred_at", _timestamp(self.occurred_at, "occurred_at")
        )

    def canonical_sha256(self) -> str:
        return _hash(
            {
                "occurrence_id": self.occurrence_id,
                "provider_id": self.provider_id,
                "action": self.action.value,
                "actor_consciousness_instance_id": self.actor_consciousness_instance_id,
                "source_instance_id": self.source_instance_id,
                "source_occurrence_ids": list(self.source_occurrence_ids),
                "causation_occurrence_id": self.causation_occurrence_id,
                "expected_revision": self.expected_revision,
                "descriptor_version": self.descriptor_version,
                "descriptor_sha256": self.descriptor_sha256,
                "workflow_id": self.workflow_id,
                "workflow_revision": self.workflow_revision,
                "workflow_sha256": self.workflow_sha256,
                "reason": self.reason,
                "occurred_at": self.occurred_at,
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    provider_id: str
    status: ProviderStatus
    revision: int
    descriptor_version: str
    descriptor_sha256: str
    workflow_id: str
    workflow_revision: int
    workflow_sha256: str
    last_occurrence_id: str
    last_event_position: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProviderCommit:
    provider_id: str
    occurrence_id: str
    revision: int
    status: ProviderStatus
    event_sha256: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ProviderPage:
    items: tuple[ProviderBinding, ...]
    continuation: str
    source_frontier: int


@dataclass(frozen=True, slots=True)
class WorkflowVersionCommand:
    """Append an exact subject-authored Skill/workflow version."""

    occurrence_id: str
    workflow_id: str
    provider_id: str
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    expected_revision: int
    schema_version: int
    content_bytes: bytes
    content_sha256: str
    reason: str
    occurred_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "occurrence_id", _text(self.occurrence_id, "occurrence_id")
        )
        object.__setattr__(self, "workflow_id", _text(self.workflow_id, "workflow_id"))
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        object.__setattr__(
            self,
            "actor_consciousness_instance_id",
            _text(
                self.actor_consciousness_instance_id, "actor_consciousness_instance_id"
            ),
        )
        object.__setattr__(
            self,
            "source_instance_id",
            _text(self.source_instance_id, "source_instance_id"),
        )
        object.__setattr__(
            self,
            "source_occurrence_ids",
            _refs(self.source_occurrence_ids, "source_occurrence_ids"),
        )
        object.__setattr__(
            self,
            "causation_occurrence_id",
            _text(self.causation_occurrence_id, "causation_occurrence_id"),
        )
        object.__setattr__(
            self,
            "expected_revision",
            _revision(self.expected_revision, "expected_revision"),
        )
        object.__setattr__(
            self,
            "schema_version",
            _revision(self.schema_version, "schema_version", positive=True),
        )
        content = bytes(self.content_bytes)
        if len(content) > _MAX_WORKFLOW_BYTES:
            raise ValueError("workflow content must be 0 bytes..8 MiB")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("workflow content must be valid UTF-8") from exc
        digest = _digest(self.content_sha256, "content_sha256")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("content_sha256 does not match workflow bytes")
        object.__setattr__(self, "content_bytes", content)
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "reason", _reason(self.reason))
        object.__setattr__(
            self, "occurred_at", _timestamp(self.occurred_at, "occurred_at")
        )

    def canonical_sha256(self) -> str:
        return _hash(
            {
                "occurrence_id": self.occurrence_id,
                "workflow_id": self.workflow_id,
                "provider_id": self.provider_id,
                "actor_consciousness_instance_id": self.actor_consciousness_instance_id,
                "source_instance_id": self.source_instance_id,
                "source_occurrence_ids": list(self.source_occurrence_ids),
                "causation_occurrence_id": self.causation_occurrence_id,
                "expected_revision": self.expected_revision,
                "schema_version": self.schema_version,
                "content_sha256": self.content_sha256,
                "content_bytes": len(self.content_bytes),
                "reason": self.reason,
                "occurred_at": self.occurred_at,
            }
        )


@dataclass(frozen=True, slots=True)
class WorkflowVersion:
    position: int
    occurrence_id: str
    workflow_id: str
    provider_id: str
    revision: int
    schema_version: int
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    content_bytes: bytes
    content_sha256: str
    reason: str
    occurred_at: str
    recorded_at: str
    event_sha256: str
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowChunk:
    workflow_id: str
    revision: int
    content_sha256: str
    offset_bytes: int
    next_offset_bytes: int
    total_bytes: int
    content: str
    complete: bool


def _schedule(
    kind: OpportunitySchedule,
    first_due_at: object,
    interval_seconds: object,
) -> tuple[str, int]:
    due = _timestamp(first_due_at, "first_due_at", allow_empty=True)
    interval = int(interval_seconds)
    if kind == OpportunitySchedule.MANUAL:
        if due or interval:
            raise ValueError("manual schedule cannot carry due fields")
    elif kind == OpportunitySchedule.AT:
        if not due or interval:
            raise ValueError("at schedule requires only first_due_at")
    elif kind == OpportunitySchedule.INTERVAL and (
        not due or not 5 <= interval <= _MAX_INTERVAL_SECONDS
    ):
        raise ValueError("interval schedule requires due time and bounded interval")
    return due, interval


@dataclass(frozen=True, slots=True)
class OpportunityRegistrationCommand:
    """One subject-authored registration lifecycle decision."""

    occurrence_id: str
    opportunity_id: str
    provider_id: str
    action: OpportunityAction
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    expected_revision: int
    referent_kind: str
    referent_id: str
    referent_revision: int
    referent_sha256: str
    workflow_id: str
    workflow_revision: int
    workflow_sha256: str
    schedule: OpportunitySchedule
    first_due_at: str
    interval_seconds: int
    reason: str
    occurred_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "occurrence_id", _text(self.occurrence_id, "occurrence_id")
        )
        object.__setattr__(
            self, "opportunity_id", _text(self.opportunity_id, "opportunity_id")
        )
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        action = OpportunityAction(self.action)
        object.__setattr__(self, "action", action)
        object.__setattr__(
            self,
            "actor_consciousness_instance_id",
            _text(
                self.actor_consciousness_instance_id, "actor_consciousness_instance_id"
            ),
        )
        object.__setattr__(
            self,
            "source_instance_id",
            _text(self.source_instance_id, "source_instance_id"),
        )
        object.__setattr__(
            self,
            "source_occurrence_ids",
            _refs(self.source_occurrence_ids, "source_occurrence_ids"),
        )
        object.__setattr__(
            self,
            "causation_occurrence_id",
            _text(self.causation_occurrence_id, "causation_occurrence_id"),
        )
        expected = _revision(self.expected_revision, "expected_revision")
        if action != OpportunityAction.OPEN and expected == 0:
            raise ValueError(
                "existing opportunity actions require expected_revision > 0"
            )
        object.__setattr__(self, "expected_revision", expected)
        object.__setattr__(
            self, "referent_kind", _text(self.referent_kind, "referent_kind", 128)
        )
        object.__setattr__(self, "referent_id", _text(self.referent_id, "referent_id"))
        object.__setattr__(
            self,
            "referent_revision",
            _revision(self.referent_revision, "referent_revision"),
        )
        object.__setattr__(
            self, "referent_sha256", _digest(self.referent_sha256, "referent_sha256")
        )
        object.__setattr__(self, "workflow_id", _text(self.workflow_id, "workflow_id"))
        object.__setattr__(
            self,
            "workflow_revision",
            _revision(self.workflow_revision, "workflow_revision", positive=True),
        )
        object.__setattr__(
            self, "workflow_sha256", _digest(self.workflow_sha256, "workflow_sha256")
        )
        schedule = OpportunitySchedule(self.schedule)
        object.__setattr__(self, "schedule", schedule)
        due, interval = _schedule(schedule, self.first_due_at, self.interval_seconds)
        object.__setattr__(self, "first_due_at", due)
        object.__setattr__(self, "interval_seconds", interval)
        object.__setattr__(self, "reason", _reason(self.reason))
        object.__setattr__(
            self, "occurred_at", _timestamp(self.occurred_at, "occurred_at")
        )

    def canonical_sha256(self) -> str:
        return _registration_hash(self, OpportunityOrigin.SUBJECT)


@dataclass(frozen=True, slots=True)
class ProviderOpportunityProposal:
    """Infrastructure availability proposal, explicitly not a subject choice."""

    occurrence_id: str
    opportunity_id: str
    provider_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    referent_kind: str
    referent_id: str
    referent_revision: int
    referent_sha256: str
    workflow_id: str
    workflow_revision: int
    workflow_sha256: str
    schedule: OpportunitySchedule
    first_due_at: str
    interval_seconds: int
    occurred_at: str

    def __post_init__(self) -> None:
        shadow = OpportunityRegistrationCommand(
            occurrence_id=self.occurrence_id,
            opportunity_id=self.opportunity_id,
            provider_id=self.provider_id,
            action=OpportunityAction.OPEN,
            actor_consciousness_instance_id="infrastructure:provider-proposal",
            source_instance_id=self.source_instance_id,
            source_occurrence_ids=self.source_occurrence_ids,
            causation_occurrence_id=self.causation_occurrence_id,
            expected_revision=0,
            referent_kind=self.referent_kind,
            referent_id=self.referent_id,
            referent_revision=self.referent_revision,
            referent_sha256=self.referent_sha256,
            workflow_id=self.workflow_id,
            workflow_revision=self.workflow_revision,
            workflow_sha256=self.workflow_sha256,
            schedule=self.schedule,
            first_due_at=self.first_due_at,
            interval_seconds=self.interval_seconds,
            reason="",
            occurred_at=self.occurred_at,
        )
        for name in (
            "occurrence_id",
            "opportunity_id",
            "provider_id",
            "source_instance_id",
            "source_occurrence_ids",
            "causation_occurrence_id",
            "referent_kind",
            "referent_id",
            "referent_revision",
            "referent_sha256",
            "workflow_id",
            "workflow_revision",
            "workflow_sha256",
            "schedule",
            "first_due_at",
            "interval_seconds",
            "occurred_at",
        ):
            object.__setattr__(self, name, getattr(shadow, name))

    def canonical_sha256(self) -> str:
        return _registration_hash(self, OpportunityOrigin.PROVIDER)


def _registration_hash(
    value: OpportunityRegistrationCommand | ProviderOpportunityProposal,
    origin: OpportunityOrigin,
) -> str:
    subject = isinstance(value, OpportunityRegistrationCommand)
    return _hash(
        {
            "occurrence_id": value.occurrence_id,
            "opportunity_id": value.opportunity_id,
            "provider_id": value.provider_id,
            "origin": origin.value,
            "action": value.action.value if subject else OpportunityAction.OPEN.value,
            "actor_consciousness_instance_id": value.actor_consciousness_instance_id
            if subject
            else "",
            "source_instance_id": value.source_instance_id,
            "source_occurrence_ids": list(value.source_occurrence_ids),
            "causation_occurrence_id": value.causation_occurrence_id,
            "expected_revision": value.expected_revision if subject else 0,
            "referent_kind": value.referent_kind,
            "referent_id": value.referent_id,
            "referent_revision": value.referent_revision,
            "referent_sha256": value.referent_sha256,
            "workflow_id": value.workflow_id,
            "workflow_revision": value.workflow_revision,
            "workflow_sha256": value.workflow_sha256,
            "schedule": value.schedule.value,
            "first_due_at": value.first_due_at,
            "interval_seconds": value.interval_seconds,
            "reason": value.reason if subject else "",
            "occurred_at": value.occurred_at,
        }
    )


@dataclass(frozen=True, slots=True)
class OpportunityRegistration:
    opportunity_id: str
    provider_id: str
    origin: OpportunityOrigin
    status: OpportunityStatus
    revision: int
    referent_kind: str
    referent_id: str
    referent_revision: int
    referent_sha256: str
    workflow_id: str
    workflow_revision: int
    workflow_sha256: str
    schedule: OpportunitySchedule
    first_due_at: str
    interval_seconds: int
    last_occurrence_id: str
    last_event_position: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class OpportunityCommit:
    opportunity_id: str
    occurrence_id: str
    revision: int
    status: OpportunityStatus
    event_sha256: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class OpportunityPage:
    items: tuple[OpportunityRegistration, ...]
    continuation: str
    source_frontier: int


@dataclass(frozen=True, slots=True)
class OpportunityOccurrence:
    """One immutable availability occurrence materialized from DB time."""

    position: int
    occurrence_id: str
    opportunity_id: str
    registration_revision: int
    provider_id: str
    provider_revision: int
    workflow_id: str
    workflow_revision: int
    workflow_sha256: str
    referent_kind: str
    referent_id: str
    referent_revision: int
    referent_sha256: str
    due_index: int
    scheduled_for: str
    available_at: str
    source_frontier: int
    occurrence_sha256: str


@dataclass(frozen=True, slots=True)
class OpportunityPublication:
    """Durable publication outbox item; no semantic outcome is implied."""

    outbox_id: str
    occurrence_id: str
    opportunity_id: str
    status: PublicationStatus
    revision: int
    life_event_occurrence_id: str
    life_event_sha256: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OpportunityDeliveryReceipt:
    """Content-free proof that a final model attempt saw the exact context."""

    receipt_id: str
    occurrence_id: str
    life_event_occurrence_id: str
    consumer_consciousness_instance_id: str
    context_delivery_id: str
    final_request_id: str
    final_attempt_id: str
    exact_present: bool
    expected_bytes: int
    effective_bytes: int
    expected_sha256: str
    effective_sha256: str
    perceived_at: str

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "occurrence_id",
            "life_event_occurrence_id",
            "consumer_consciousness_instance_id",
            "context_delivery_id",
            "final_request_id",
            "final_attempt_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self, "expected_bytes", _revision(self.expected_bytes, "expected_bytes")
        )
        object.__setattr__(
            self, "effective_bytes", _revision(self.effective_bytes, "effective_bytes")
        )
        object.__setattr__(
            self, "expected_sha256", _digest(self.expected_sha256, "expected_sha256")
        )
        object.__setattr__(
            self, "effective_sha256", _digest(self.effective_sha256, "effective_sha256")
        )
        object.__setattr__(
            self, "perceived_at", _timestamp(self.perceived_at, "perceived_at")
        )

    def canonical_sha256(self) -> str:
        return _hash(
            {
                "receipt_id": self.receipt_id,
                "occurrence_id": self.occurrence_id,
                "life_event_occurrence_id": self.life_event_occurrence_id,
                "consumer_consciousness_instance_id": self.consumer_consciousness_instance_id,
                "context_delivery_id": self.context_delivery_id,
                "final_request_id": self.final_request_id,
                "final_attempt_id": self.final_attempt_id,
                "exact_present": bool(self.exact_present),
                "expected_bytes": self.expected_bytes,
                "effective_bytes": self.effective_bytes,
                "expected_sha256": self.expected_sha256,
                "effective_sha256": self.effective_sha256,
                "perceived_at": self.perceived_at,
            }
        )


@dataclass(frozen=True, slots=True)
class OpportunityDeliveryRecord:
    position: int
    receipt: OpportunityDeliveryReceipt
    recorded_at: str
    receipt_sha256: str
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class OpportunityDeliveryCommit:
    """An immutable fact commit; scheduler projection is intentionally separate."""

    record: OpportunityDeliveryRecord


@dataclass(frozen=True, slots=True)
class OpportunityHistoryRecord:
    family: OpportunityHistoryFamily
    position: int
    occurrence_id: str
    aggregate_id: str
    action: str
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    expected_revision: int
    revision: int
    reason_bytes: int
    reason_sha256: str
    occurred_at: str
    recorded_at: str
    event_sha256: str


@dataclass(frozen=True, slots=True)
class OpportunityHistoryPage:
    items: tuple[OpportunityHistoryRecord, ...]
    continuation: str
    source_frontier: int


@dataclass(frozen=True, slots=True)
class OpportunityHistoryChunk:
    family: OpportunityHistoryFamily
    occurrence_id: str
    offset_bytes: int
    next_offset_bytes: int
    total_bytes: int
    reason_sha256: str
    content: str
    complete: bool


@runtime_checkable
class OpportunityAuthorityPort(Protocol):
    async def manage_provider(
        self, command: ProviderBindingCommand
    ) -> ProviderCommit: ...

    async def append_workflow(
        self, command: WorkflowVersionCommand
    ) -> WorkflowVersion: ...

    async def decide_opportunity(
        self, command: OpportunityRegistrationCommand
    ) -> OpportunityCommit: ...

    async def propose_opportunity(
        self, proposal: ProviderOpportunityProposal
    ) -> OpportunityCommit: ...

    async def get_provider(self, provider_id: str) -> ProviderBinding | None: ...

    async def get_workflow(
        self, workflow_id: str, revision: int
    ) -> WorkflowVersion | None: ...

    async def read_workflow_chunk(
        self,
        workflow_id: str,
        revision: int,
        *,
        offset_bytes: int,
        max_bytes: int,
    ) -> WorkflowChunk: ...

    async def get_opportunity(
        self, opportunity_id: str
    ) -> OpportunityRegistration | None: ...

    async def page_providers(
        self,
        *,
        limit: int = 100,
        continuation: str = "",
    ) -> ProviderPage: ...

    async def page_opportunities(
        self,
        *,
        limit: int = 100,
        continuation: str = "",
    ) -> OpportunityPage: ...

    async def page_history(
        self,
        family: OpportunityHistoryFamily,
        *,
        aggregate_id: str = "",
        limit: int = 100,
        continuation: str = "",
    ) -> OpportunityHistoryPage: ...

    async def read_history_reason_chunk(
        self,
        family: OpportunityHistoryFamily,
        occurrence_id: str,
        *,
        offset_bytes: int,
        max_bytes: int,
    ) -> OpportunityHistoryChunk: ...

    async def health_snapshot(self) -> dict[str, Any]: ...


@runtime_checkable
class OpportunitySchedulerPort(Protocol):
    async def materialize_due(
        self, *, limit: int = 100
    ) -> tuple[OpportunityOccurrence, ...]: ...

    async def pending_publications(
        self, *, limit: int = 100
    ) -> tuple[OpportunityPublication, ...]: ...

    async def awaiting_delivery(
        self,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityPublication, ...]: ...

    async def mark_published(
        self,
        outbox_id: str,
        *,
        expected_revision: int,
        life_event_sha256: str,
    ) -> OpportunityPublication: ...

    async def list_occurrences(
        self,
        opportunity_id: str,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityOccurrence, ...]: ...

    async def get_occurrence(
        self,
        occurrence_id: str,
    ) -> OpportunityOccurrence | None: ...

    async def reconcile_deliveries(self, *, limit: int = 100) -> tuple[str, ...]: ...

    async def scheduler_health_snapshot(self) -> dict[str, Any]: ...


@runtime_checkable
class OpportunityDeliveryPort(Protocol):
    async def awaiting_delivery(
        self,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityPublication, ...]: ...

    async def commit_exact(
        self, receipt: OpportunityDeliveryReceipt
    ) -> OpportunityDeliveryCommit: ...

    async def list_deliveries(
        self,
        opportunity_id: str,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityDeliveryRecord, ...]: ...

    async def get_publication(
        self,
        life_event_occurrence_id: str,
    ) -> OpportunityPublication | None: ...

    async def latest_publication(
        self,
        opportunity_id: str,
    ) -> OpportunityPublication | None: ...


@dataclass(frozen=True, slots=True)
class OpportunityStores:
    authority: OpportunityAuthorityPort
    scheduler: OpportunitySchedulerPort | None
    delivery: OpportunityDeliveryPort


__all__ = [
    "OPPORTUNITY_MANAGED_MARKER_KEY",
    "OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE",
    "OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY",
    "OpportunityAction",
    "OpportunityActorInactive",
    "OpportunityAuthorityPort",
    "OpportunityCommit",
    "OpportunityConflict",
    "OpportunityDeliveryCommit",
    "OpportunityDeliveryPort",
    "OpportunityDeliveryReceipt",
    "OpportunityDeliveryRecord",
    "OpportunityDeliveryRejected",
    "OpportunityHistoryChunk",
    "OpportunityHistoryFamily",
    "OpportunityHistoryPage",
    "OpportunityHistoryRecord",
    "OpportunityOccurrence",
    "OpportunityOrigin",
    "OpportunityPage",
    "OpportunityPublication",
    "OpportunityRegistration",
    "OpportunityRegistrationCommand",
    "OpportunityRuntimeMarker",
    "OpportunitySchedule",
    "OpportunitySchedulerClaimRequired",
    "OpportunitySchedulerPort",
    "OpportunityStatus",
    "OpportunityStores",
    "OpportunityTransitionError",
    "ProviderAction",
    "ProviderBinding",
    "ProviderBindingCommand",
    "ProviderCommit",
    "ProviderOpportunityProposal",
    "ProviderPage",
    "ProviderStatus",
    "PublicationStatus",
    "WorkflowChunk",
    "WorkflowVersion",
    "WorkflowVersionCommand",
]
