"""Contracts for subject-authored initiative.

An initiative seed keeps one subject-authored intention available to future
consciousness instances. It never contains a platform, chat stream, prepared
reply, importance score, recurrence rule, or infrastructure-authored meaning.
Audience and delivery surface are separate decisions made only when acting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

InitiativeSeedAction = Literal["hold", "rewrite", "reencounter", "release"]
InitiativeSeedStatus = Literal["open", "released"]
INITIATIVE_SEED_ACTIONS = frozenset(
    {"hold", "rewrite", "reencounter", "release"}
)
INITIATIVE_MAX_STATEMENT_BYTES = 1024 * 1024
INITIATIVE_MAX_ENTITY_REFS = 64
INITIATIVE_MAX_REENCOUNTER_MINUTES = 365 * 24 * 60


class InitiativeConflict(RuntimeError):
    """An occurrence was reused or an expected revision became stale."""

    def __init__(
        self,
        message: str,
        *,
        seed_id: str = "",
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.seed_id = seed_id
        self.current_revision = current_revision


class InitiativeActorInactive(RuntimeError):
    """The claimed consciousness instance is not currently active."""


class InitiativeTransitionError(RuntimeError):
    """An explicit subject transition is invalid."""


class InitiativeSurfaceUnavailable(RuntimeError):
    """An explicitly selected physical surface is no longer valid."""


def _text(value: object, name: str, limit: int = 512) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    if len(result) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return result


def _timestamp(value: object, name: str) -> str:
    raw = _text(value, name, 128)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _refs(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    result = tuple(_text(value, name) for value in values)
    if len(result) > INITIATIVE_MAX_ENTITY_REFS:
        raise ValueError(f"{name} exceeds {INITIATIVE_MAX_ENTITY_REFS} entries")
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must contain unique refs")
    return result


def _digest(material: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class InitiativeSeedCommand:
    """One explicit decision about future subject continuity."""

    occurrence_id: str
    seed_id: str
    action: InitiativeSeedAction
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    expected_revision: int
    public_statement: str
    related_entity_refs: tuple[str, ...]
    occurred_at: str
    reencounter_after_minutes: int = 0

    def __post_init__(self) -> None:
        action = str(self.action or "").strip()
        if action not in INITIATIVE_SEED_ACTIONS:
            raise ValueError(f"unsupported initiative action: {action}")
        revision = int(self.expected_revision)
        if revision < 0:
            raise ValueError("initiative expected_revision must not be negative")
        if (action == "hold") != (revision == 0):
            raise ValueError(
                "hold requires revision zero; existing actions require a revision"
            )
        statement = str(self.public_statement or "")
        if action in {"hold", "rewrite", "release"} and not statement.strip():
            raise ValueError(f"initiative action '{action}' requires a statement")
        if action == "reencounter" and statement:
            raise ValueError("reencounter cannot smuggle a new subject statement")
        if len(statement.encode()) > INITIATIVE_MAX_STATEMENT_BYTES:
            raise ValueError("initiative statement exceeds its byte limit")
        minutes = int(self.reencounter_after_minutes)
        if action == "reencounter":
            if not 1 <= minutes <= INITIATIVE_MAX_REENCOUNTER_MINUTES:
                raise ValueError("initiative reencounter delay is out of range")
        elif minutes:
            raise ValueError("only reencounter accepts a delay")
        for field, value in (
            ("occurrence_id", _text(self.occurrence_id, "occurrence_id")),
            ("seed_id", _text(self.seed_id, "seed_id")),
            (
                "actor_consciousness_instance_id",
                _text(self.actor_consciousness_instance_id, "actor"),
            ),
            ("source_instance_id", _text(self.source_instance_id, "source instance")),
            (
                "causation_occurrence_id",
                _text(self.causation_occurrence_id, "causation"),
            ),
        ):
            object.__setattr__(self, field, value)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "expected_revision", revision)
        object.__setattr__(self, "public_statement", statement)
        source_refs = _refs(self.source_occurrence_ids, "source occurrence")
        related_refs = _refs(self.related_entity_refs, "related entity")
        if action == "reencounter" and related_refs:
            raise ValueError("reencounter cannot change related entity refs")
        object.__setattr__(self, "source_occurrence_ids", source_refs)
        object.__setattr__(self, "related_entity_refs", related_refs)
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "reencounter_after_minutes", minutes)

    @property
    def revision(self) -> int:
        return self.expected_revision + 1

    def reencounter_at(self) -> str:
        if self.action != "reencounter":
            return ""
        return (
            datetime.fromisoformat(self.occurred_at)
            + timedelta(minutes=self.reencounter_after_minutes)
        ).isoformat()

    def canonical_sha256(self) -> str:
        return _digest(
            {
                "occurrence_id": self.occurrence_id,
                "seed_id": self.seed_id,
                "action": self.action,
                "actor": self.actor_consciousness_instance_id,
                "source_instance_id": self.source_instance_id,
                "source_occurrence_ids": list(self.source_occurrence_ids),
                "causation_occurrence_id": self.causation_occurrence_id,
                "expected_revision": self.expected_revision,
                "public_statement": self.public_statement,
                "related_entity_refs": list(self.related_entity_refs),
                "occurred_at": self.occurred_at,
                "reencounter_after_minutes": self.reencounter_after_minutes,
            }
        )


@dataclass(frozen=True, slots=True)
class InitiativeSeedView:
    """Current view rebuilt only from immutable subject decisions."""

    seed_id: str
    status: InitiativeSeedStatus
    revision: int
    current_statement: str
    related_entity_refs: tuple[str, ...]
    opened_at: str
    last_changed_at: str
    last_event_position: int
    last_event_id: str
    last_occurrence_id: str
    reencounter_at: str = ""
    reencounter_revision: int = 0
    reencounter_event_id: str = ""
    reencounter_delivered_at: str = ""
    reencounter_delivery_event_id: str = ""
    content_event_id: str = ""
    content_revision: int = 0


@dataclass(frozen=True, slots=True)
class InitiativeSeedCommit:
    """Content-free result of a subject decision."""

    event_id: str
    occurrence_id: str
    seed_id: str
    revision: int
    status: InitiativeSeedStatus
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class InitiativeReencounterReceipt:
    """Content-free receipt for one technical seed re-delivery."""

    event_id: str
    occurrence_id: str
    seed_id: str
    seed_revision: int
    life_event_id: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class InitiativeOutreachCommand:
    """Explicit audience and physical-surface choice made at action time."""

    occurrence_id: str
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    audience_ref: str
    surface_ref: str
    public_intention: str
    occurred_at: str
    seed_id: str = ""
    seed_revision: int = 0

    def __post_init__(self) -> None:
        for field, value in (
            ("occurrence_id", _text(self.occurrence_id, "occurrence_id")),
            (
                "actor_consciousness_instance_id",
                _text(self.actor_consciousness_instance_id, "actor"),
            ),
            ("source_instance_id", _text(self.source_instance_id, "source instance")),
            (
                "causation_occurrence_id",
                _text(self.causation_occurrence_id, "causation"),
            ),
            ("audience_ref", _text(self.audience_ref, "audience_ref")),
            ("surface_ref", _text(self.surface_ref, "surface_ref")),
        ):
            object.__setattr__(self, field, value)
        object.__setattr__(
            self,
            "source_occurrence_ids",
            _refs(self.source_occurrence_ids, "source occurrence"),
        )
        intention = _text(self.public_intention, "public_intention", 4096)
        if len(intention.encode()) > 16 * 1024:
            raise ValueError("outreach intention exceeds its byte limit")
        object.__setattr__(self, "public_intention", intention)
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        seed_id = str(self.seed_id or "").strip()
        seed_revision = int(self.seed_revision)
        if seed_revision < 0:
            raise ValueError("seed_revision must not be negative")
        if bool(seed_id) != bool(seed_revision):
            raise ValueError("seed_id and seed_revision must be supplied together")
        object.__setattr__(self, "seed_id", seed_id)
        object.__setattr__(self, "seed_revision", seed_revision)

    def canonical_sha256(self) -> str:
        return _digest(
            {
                "occurrence_id": self.occurrence_id,
                "actor": self.actor_consciousness_instance_id,
                "source_instance_id": self.source_instance_id,
                "source_occurrence_ids": list(self.source_occurrence_ids),
                "causation_occurrence_id": self.causation_occurrence_id,
                "audience_ref": self.audience_ref,
                "surface_ref": self.surface_ref,
                "public_intention": self.public_intention,
                "occurred_at": self.occurred_at,
                "seed_id": self.seed_id,
                "seed_revision": self.seed_revision,
            }
        )


@dataclass(frozen=True, slots=True)
class InitiativeOutreachReceipt:
    """Content-free authority receipt; sending remains a separate boundary."""

    event_id: str
    occurrence_id: str
    audience_ref: str
    surface_ref: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ReachableSurface:
    """Verified physical route kept internal to the embodiment boundary."""

    surface_ref: str
    audience_ref: str
    platform: str
    chat_type: Literal["private", "group"]
    display_name: str
    stream_id: str

    def public_projection(self) -> dict[str, str]:
        return {
            "surface_ref": self.surface_ref,
            "audience_ref": self.audience_ref,
            "platform": self.platform,
            "chat_type": self.chat_type,
            "display_name": self.display_name,
        }


@runtime_checkable
class InitiativeAuthorityPort(Protocol):
    async def decide_seed(
        self, command: InitiativeSeedCommand
    ) -> InitiativeSeedCommit: ...
    async def get_seed(self, seed_id: str) -> InitiativeSeedView | None: ...
    async def list_seeds(
        self, *, include_released: bool = False
    ) -> tuple[InitiativeSeedView, ...]: ...
    async def due_reencounters(
        self, *, now: str
    ) -> tuple[InitiativeSeedView, ...]: ...
    async def record_reencounter_delivery(
        self,
        *,
        seed_id: str,
        seed_revision: int,
        life_event_id: str,
        occurred_at: str,
    ) -> InitiativeReencounterReceipt: ...
    async def begin_outreach(
        self, command: InitiativeOutreachCommand
    ) -> InitiativeOutreachReceipt: ...
