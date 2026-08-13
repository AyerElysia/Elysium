"""Canonical subject-level contracts for persistent attention threads.

Attention threads retain only statements that a live consciousness instance
explicitly chose to make durable.  They are not hidden reasoning, a task queue,
or an infrastructure-computed importance ranking.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

AttentionThreadAction = Literal["open", "note", "pause", "resume", "close"]
AttentionThreadStatus = Literal["open", "paused", "closed"]

ATTENTION_THREAD_ACTIONS = frozenset({"open", "note", "pause", "resume", "close"})
ATTENTION_THREAD_STATUSES = frozenset({"open", "paused", "closed"})
ATTENTION_THREAD_MAX_STATEMENT_BYTES = 1024 * 1024
ATTENTION_THREAD_MAX_PAGE_BYTES = 256 * 1024
ATTENTION_THREAD_MIN_PAGE_BYTES = 4 * 1024


class AttentionThreadConflict(RuntimeError):
    """Raised for occurrence reuse, stale revision, or concurrent mutation.

    ``thread_id`` / ``current_revision`` / ``thread_exists`` are optional
    structured hints so the subject tool can recover without guessing;
    message-only construction stays supported for internal callers.
    """

    def __init__(
        self,
        message: str,
        *,
        thread_id: str = "",
        current_revision: int | None = None,
        thread_exists: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.thread_id = thread_id
        self.current_revision = current_revision
        self.thread_exists = thread_exists


class AttentionThreadActorInactive(RuntimeError):
    """Raised when the deciding consciousness instance is not active."""


class AttentionThreadTransitionError(RuntimeError):
    """Raised when an explicit action is invalid for the current status."""


class AttentionThreadProjectionConflict(RuntimeError):
    """Raised when a continuation no longer matches its source projection."""


def _required_text(value: object, *, field_name: str, max_chars: int = 255) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    if len(text) > max_chars:
        raise ValueError(f"{field_name} exceeds {max_chars} characters")
    return text


def _iso_timestamp(value: object, *, field_name: str) -> str:
    text = _required_text(value, field_name=field_name, max_chars=128)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.isoformat()


def _sha256(value: object, *, field_name: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{field_name} must be 64 hexadecimal characters")
    return digest


@dataclass(frozen=True, slots=True)
class AttentionThreadCommand:
    """One explicit subject decision submitted to the authority boundary."""

    occurrence_id: str
    thread_id: str
    action: AttentionThreadAction
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    expected_revision: int
    public_statement: str
    occurred_at: str

    def __post_init__(self) -> None:
        occurrence_id = _required_text(
            self.occurrence_id,
            field_name="attention occurrence_id",
        )
        thread_id = _required_text(self.thread_id, field_name="attention thread_id")
        actor = _required_text(
            self.actor_consciousness_instance_id,
            field_name="attention actor_consciousness_instance_id",
        )
        source_instance = _required_text(
            self.source_instance_id,
            field_name="attention source_instance_id",
        )
        causation = _required_text(
            self.causation_occurrence_id,
            field_name="attention causation_occurrence_id",
        )
        action = str(self.action or "").strip()
        if action not in ATTENTION_THREAD_ACTIONS:
            raise ValueError(f"unsupported attention action: {action}")
        revision = int(self.expected_revision)
        if revision < 0:
            raise ValueError("attention expected_revision must not be negative")
        if action == "open" and revision != 0:
            raise ValueError("opening an attention thread requires expected_revision=0")
        if action != "open" and revision == 0:
            raise ValueError("existing attention thread actions require a revision")
        statement = str(self.public_statement or "")
        statement_bytes = len(statement.encode("utf-8"))
        if action in {"open", "note", "close"} and not statement.strip():
            raise ValueError(f"attention action '{action}' requires public_statement")
        if statement_bytes > ATTENTION_THREAD_MAX_STATEMENT_BYTES:
            raise ValueError("attention public_statement exceeds its storage byte limit")
        source_occurrences = tuple(
            _required_text(value, field_name="attention source occurrence")
            for value in self.source_occurrence_ids
        )
        if len(source_occurrences) > 128:
            raise ValueError("attention source occurrence list exceeds 128 entries")
        if len(set(source_occurrences)) != len(source_occurrences):
            raise ValueError("attention source occurrences must be unique")
        occurred_at = _iso_timestamp(
            self.occurred_at,
            field_name="attention occurred_at",
        )
        object.__setattr__(self, "occurrence_id", occurrence_id)
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "actor_consciousness_instance_id", actor)
        object.__setattr__(self, "source_instance_id", source_instance)
        object.__setattr__(self, "source_occurrence_ids", source_occurrences)
        object.__setattr__(self, "causation_occurrence_id", causation)
        object.__setattr__(self, "expected_revision", revision)
        object.__setattr__(self, "public_statement", statement)
        object.__setattr__(self, "occurred_at", occurred_at)

    def canonical_sha256(self) -> str:
        """Return the immutable command identity used for idempotency checks."""

        material = {
            "occurrence_id": self.occurrence_id,
            "thread_id": self.thread_id,
            "action": self.action,
            "actor_consciousness_instance_id": (
                self.actor_consciousness_instance_id
            ),
            "source_instance_id": self.source_instance_id,
            "source_occurrence_ids": list(self.source_occurrence_ids),
            "causation_occurrence_id": self.causation_occurrence_id,
            "expected_revision": self.expected_revision,
            "public_statement": self.public_statement,
            "occurred_at": self.occurred_at,
        }
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AttentionThreadEvent:
    """One positioned immutable subject decision."""

    position: int
    event_id: str
    occurrence_id: str
    thread_id: str
    action: AttentionThreadAction
    actor_consciousness_instance_id: str
    source_instance_id: str
    source_occurrence_ids: tuple[str, ...]
    causation_occurrence_id: str
    expected_revision: int
    revision: int
    public_statement: str
    occurred_at: str
    recorded_at: str
    event_sha256: str

    def __post_init__(self) -> None:
        if self.position <= 0:
            raise ValueError("attention event position must be positive")
        if self.revision != self.expected_revision + 1:
            raise ValueError("attention event revision must follow expected_revision")
        event_id = _required_text(self.event_id, field_name="attention event_id")
        command = AttentionThreadCommand(
            occurrence_id=self.occurrence_id,
            thread_id=self.thread_id,
            action=self.action,
            actor_consciousness_instance_id=(
                self.actor_consciousness_instance_id
            ),
            source_instance_id=self.source_instance_id,
            source_occurrence_ids=self.source_occurrence_ids,
            causation_occurrence_id=self.causation_occurrence_id,
            expected_revision=self.expected_revision,
            public_statement=self.public_statement,
            occurred_at=self.occurred_at,
        )
        recorded_at = _iso_timestamp(
            self.recorded_at,
            field_name="attention recorded_at",
        )
        event_sha256 = _sha256(
            self.event_sha256,
            field_name="attention event_sha256",
        )
        if event_sha256 != command.canonical_sha256():
            raise ValueError("attention event_sha256 does not match its command")
        object.__setattr__(self, "event_id", event_id)
        for field_name in (
            "occurrence_id",
            "thread_id",
            "action",
            "actor_consciousness_instance_id",
            "source_instance_id",
            "source_occurrence_ids",
            "causation_occurrence_id",
            "expected_revision",
            "public_statement",
            "occurred_at",
        ):
            object.__setattr__(self, field_name, getattr(command, field_name))
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "event_sha256", event_sha256)


@dataclass(frozen=True, slots=True)
class AttentionThreadView:
    """Rebuildable current view derived only from immutable events."""

    thread_id: str
    status: AttentionThreadStatus
    revision: int
    opened_at: str
    last_changed_at: str
    current_statement: str
    statement_event_id: str
    statement_sha256: str
    statement_bytes: int
    last_event_id: str
    last_occurrence_id: str
    last_event_position: int

    def __post_init__(self) -> None:
        _required_text(self.thread_id, field_name="attention thread_id")
        if self.status not in ATTENTION_THREAD_STATUSES:
            raise ValueError("attention view contains an unsupported status")
        if self.revision <= 0 or self.last_event_position <= 0:
            raise ValueError("attention view revision/position must be positive")
        _iso_timestamp(self.opened_at, field_name="attention opened_at")
        _iso_timestamp(self.last_changed_at, field_name="attention last_changed_at")
        _required_text(
            self.statement_event_id,
            field_name="attention statement_event_id",
        )
        _required_text(self.last_event_id, field_name="attention last_event_id")
        _required_text(
            self.last_occurrence_id,
            field_name="attention last_occurrence_id",
        )
        statement_sha256 = _sha256(
            self.statement_sha256,
            field_name="attention statement_sha256",
        )
        encoded = self.current_statement.encode("utf-8")
        if len(encoded) != self.statement_bytes:
            raise ValueError("attention statement_bytes does not match content")
        if hashlib.sha256(encoded).hexdigest() != statement_sha256:
            raise ValueError("attention statement_sha256 does not match content")


@dataclass(frozen=True, slots=True)
class AttentionThreadCommit:
    """Content-free result of one authority decision."""

    event_id: str
    occurrence_id: str
    thread_id: str
    revision: int
    status: AttentionThreadStatus
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class InstanceFocus:
    """Ephemeral instance-local focus that cannot mutate subject authority."""

    instance_id: str
    focus_occurrence_id: str
    source_occurrence_id: str
    entered_at: str
    expires_at: str
    revision: int
    thread_id: str = ""

    def __post_init__(self) -> None:
        _required_text(self.instance_id, field_name="focus instance_id")
        _required_text(
            self.focus_occurrence_id,
            field_name="focus occurrence_id",
        )
        _required_text(
            self.source_occurrence_id,
            field_name="focus source_occurrence_id",
        )
        entered_at = _iso_timestamp(self.entered_at, field_name="focus entered_at")
        expires_at = _iso_timestamp(self.expires_at, field_name="focus expires_at")
        if datetime.fromisoformat(expires_at) <= datetime.fromisoformat(entered_at):
            raise ValueError("focus expires_at must be after entered_at")
        if self.revision <= 0:
            raise ValueError("focus revision must be positive")


@dataclass(frozen=True, slots=True)
class AttentionThreadPageQuery:
    """Bounded current-view request with an explicit stable continuation."""

    statuses: tuple[AttentionThreadStatus, ...] = ()
    continuation: str = ""
    limit: int = 32
    max_bytes: int = 32 * 1024
    projection_kind: str = "default"
    focus_instance_id: str = ""

    def __post_init__(self) -> None:
        statuses = tuple(str(value).strip() for value in self.statuses)
        if any(value not in ATTENTION_THREAD_STATUSES for value in statuses):
            raise ValueError("attention page contains an unsupported status")
        if len(set(statuses)) != len(statuses):
            raise ValueError("attention page statuses must be unique")
        continuation = str(self.continuation or "").strip()
        if len(continuation) > 4096:
            raise ValueError("attention continuation exceeds 4096 characters")
        if not 1 <= int(self.limit) <= 100:
            raise ValueError("attention page limit must be between 1 and 100")
        if not ATTENTION_THREAD_MIN_PAGE_BYTES <= int(
            self.max_bytes
        ) <= ATTENTION_THREAD_MAX_PAGE_BYTES:
            raise ValueError("attention page max_bytes is outside the supported range")
        projection_kind = _required_text(
            self.projection_kind,
            field_name="attention projection_kind",
            max_chars=64,
        )
        object.__setattr__(self, "statuses", statuses)
        object.__setattr__(self, "continuation", continuation)
        object.__setattr__(self, "limit", int(self.limit))
        object.__setattr__(self, "max_bytes", int(self.max_bytes))
        object.__setattr__(self, "projection_kind", projection_kind)
        focus_instance_id = str(self.focus_instance_id or "").strip()
        if len(focus_instance_id) > 255:
            raise ValueError("attention focus_instance_id exceeds 255 characters")
        object.__setattr__(self, "focus_instance_id", focus_instance_id)


@dataclass(frozen=True, slots=True)
class AttentionThreadProjectionItem:
    """Bounded thread reference suitable for prompts and interfaces."""

    thread_id: str
    status: AttentionThreadStatus
    revision: int
    last_event_position: int
    statement_event_id: str
    statement_sha256: str
    statement_bytes: int
    statement_excerpt: str
    excerpt_bytes: int
    excerpt_complete: bool


@dataclass(frozen=True, slots=True)
class AttentionThreadPage:
    """Traceable bounded projection; authority remains in immutable events."""

    items: tuple[AttentionThreadProjectionItem, ...]
    source_frontier: int
    projection_revision: int
    projection_sha256: str
    algorithm_version: str
    projection_kind: str
    original_bytes: int
    delivered_bytes: int
    omitted_count: int
    continuation: str
    content: str


@dataclass(frozen=True, slots=True)
class AttentionThreadEventPage:
    """Stable immutable event page; statements remain exact and attributable."""

    items: tuple[AttentionThreadEvent, ...]
    source_frontier: int
    next_position: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class AttentionThreadValueChunk:
    """Lossless UTF-8 chunk of one immutable public statement."""

    event_id: str
    offset_bytes: int
    next_offset_bytes: int
    total_bytes: int
    statement_sha256: str
    content: str
    complete: bool


@runtime_checkable
class AttentionThreadAuthorityPort(Protocol):
    """Only formal write/read boundary for subject-level attention threads."""

    async def decide(
        self,
        command: AttentionThreadCommand,
    ) -> AttentionThreadCommit:
        """Validate active actor evidence and append one immutable decision."""

    async def get(self, thread_id: str) -> AttentionThreadView | None:
        """Return the current rebuildable view without creating a thread."""

    async def page(self, query: AttentionThreadPageQuery) -> AttentionThreadPage:
        """Return a bounded traceable projection with stable continuation."""

    async def event_page(
        self,
        thread_id: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> AttentionThreadEventPage:
        """Read immutable events strictly after one position."""

    async def read_statement_chunk(
        self,
        event_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 16 * 1024,
    ) -> AttentionThreadValueChunk:
        """Read an exact public statement on UTF-8 byte boundaries."""

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free frontier, lag, capacity, and failure diagnostics."""


@runtime_checkable
class InstanceFocusPort(Protocol):
    """Technical instance-focus lifecycle; it cannot mutate thread status."""

    async def set_focus(self, focus: InstanceFocus) -> InstanceFocus:
        """Set one instance's explicit ephemeral focus."""

    async def get_focus(self, instance_id: str) -> InstanceFocus | None:
        """Return a live unexpired focus, if present."""

    async def clear_focus(
        self,
        instance_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """CAS-clear focus without touching subject authority."""


__all__ = [
    "ATTENTION_THREAD_ACTIONS",
    "ATTENTION_THREAD_MAX_PAGE_BYTES",
    "ATTENTION_THREAD_MAX_STATEMENT_BYTES",
    "ATTENTION_THREAD_MIN_PAGE_BYTES",
    "ATTENTION_THREAD_STATUSES",
    "AttentionThreadAction",
    "AttentionThreadActorInactive",
    "AttentionThreadAuthorityPort",
    "AttentionThreadCommand",
    "AttentionThreadCommit",
    "AttentionThreadConflict",
    "AttentionThreadEvent",
    "AttentionThreadEventPage",
    "AttentionThreadPage",
    "AttentionThreadPageQuery",
    "AttentionThreadProjectionConflict",
    "AttentionThreadProjectionItem",
    "AttentionThreadStatus",
    "AttentionThreadTransitionError",
    "AttentionThreadValueChunk",
    "AttentionThreadView",
    "InstanceFocus",
    "InstanceFocusPort",
]
