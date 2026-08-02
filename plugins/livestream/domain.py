"""Versioned livestream domain contracts.

The contracts distinguish immutable platform observations from subjective
director decisions and technical playback receipts. Technical schemas may be
closed and validated; cognitive meaning stays in open text fields authored by
the livestream consciousness.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

LIVESTREAM_PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class PlatformEvent:
    """One immutable observation received from a livestream platform.

    ``event_id`` is the adapter's best stable identity. ``dedup_key`` is set
    only when the source exposes enough data to prove replay equivalence. An
    adapter must not invent a stable identity when the platform did not provide
    one; such events use a UUID and remain explicitly non-deduplicable.
    """

    kind: str
    user_name: str
    content: str = ""
    value: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    platform: str = "bilibili"
    room_id: str = ""
    received_at: float = field(default_factory=time.time)
    source_sequence: str | None = None
    dedup_key: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("kind", "event_id", "platform"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"PlatformEvent.{name} must not be empty")
        if self.timestamp <= 0 or self.received_at <= 0:
            raise ValueError("PlatformEvent timestamps must be positive")

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-compatible authoritative payload."""

        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PlatformEvent:
        """Restore an event from the authoritative ledger payload."""

        return cls(**payload)

    @property
    def display_text(self) -> str:
        """Render factual event content without assigning cognitive meaning."""

        if self.content:
            return f"{self.user_name}: {self.content}"
        return f"{self.user_name}: [{self.kind}]"


class PerformancePlan(BaseModel):
    """A livestream-consciousness decision expressed as a technical plan."""

    model_config = ConfigDict(extra="forbid")

    should_speak: bool
    reason: str = Field(min_length=1, max_length=2000)
    speech_text: str = Field(default="", max_length=4000)
    addressed_event_ids: list[str] = Field(default_factory=list, max_length=200)
    addressee: str = Field(default="", max_length=200)
    expression_hint: str = Field(default="", max_length=200)
    motion_hint: str = Field(default="", max_length=200)
    scene_cue: str = Field(default="", max_length=200)
    interrupt_current: bool = False
    interruptible: bool = True

    @model_validator(mode="after")
    def validate_speech_consistency(self) -> PerformancePlan:
        """Reject ambiguous plans instead of manufacturing a fallback."""

        self.speech_text = self.speech_text.strip()
        self.reason = self.reason.strip()
        if self.should_speak and not self.speech_text:
            raise ValueError("speech_text is required when should_speak is true")
        if not self.should_speak and self.speech_text:
            raise ValueError("speech_text must be empty when should_speak is false")
        return self


class WorldPerceptionCheckpoint(BaseModel):
    """Replayable cursor window delivered to one consciousness instance."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1)
    from_position: int = Field(ge=0)
    through_position: int = Field(ge=0)
    cursor_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_window(self) -> WorldPerceptionCheckpoint:
        if self.through_position < self.from_position:
            raise ValueError("world perception window moves backwards")
        return self


class DirectorDecision(BaseModel):
    """Auditable decision made by the livestream consciousness."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    session_id: str
    actor: str
    created_at: float
    source_event_ids: list[str]
    source_record_sequences: list[int]
    life_context_high_water: int = Field(default=0, ge=0)
    world_perception: WorldPerceptionCheckpoint | None = None
    plan: PerformancePlan


class PlaybackReceipt(BaseModel):
    """What a stage client reports it actually played."""

    model_config = ConfigDict(extra="forbid")

    playback_id: str
    utterance_id: str
    chunk_id: str
    outcome: str
    started_at: float | None = None
    ended_at: float = Field(default_factory=time.time)
    played_ms: int = Field(default=0, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    detail: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_outcome(self) -> PlaybackReceipt:
        allowed = {"completed", "interrupted", "failed", "timed_out"}
        if self.outcome not in allowed:
            raise ValueError(f"unsupported playback outcome: {self.outcome}")
        return self


class StageMessage(BaseModel):
    """Versioned JSON control frame sent to a browser or OBS stage client."""

    model_config = ConfigDict(extra="forbid")

    version: int = LIVESTREAM_PROTOCOL_VERSION
    type: str
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)


class HealthSnapshot(BaseModel):
    """Read-only, non-secret runtime health contract."""

    model_config = ConfigDict(extra="forbid")

    status: str
    session_id: str | None = None
    platform_connected: bool = False
    stage_clients: int = 0
    primary_stage_connected: bool = False
    event_backlog: int = 0
    performance_backlog: int = 0
    current_utterance_id: str | None = None
    last_platform_event_at: float | None = None
    last_decision_at: float | None = None
    last_playback_completed_at: float | None = None
    degraded_reasons: list[str] = Field(default_factory=list)
