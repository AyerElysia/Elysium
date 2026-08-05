"""P3-08 livestream public schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from .commands import CommandResponse
from .common import StrictModel, VersionedModel


def _timestamp(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _required_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


class LivestreamStatus(VersionedModel):
    status: Literal["stopped", "starting", "running", "stopping", "failed", "degraded"]
    session_id: str | None = None
    platform_connected: bool = False
    stage_clients: int = Field(default=0, ge=0)
    primary_stage_connected: bool = False
    event_backlog: int = Field(default=0, ge=0)
    performance_backlog: int = Field(default=0, ge=0)
    current_utterance_id: str | None = None
    last_platform_event_at: datetime | None = None
    last_decision_at: datetime | None = None
    last_playback_completed_at: datetime | None = None
    degraded_reasons: tuple[str, ...] = ()


class LivestreamSessionSummary(VersionedModel):
    session_id: str
    platform: str | None = None
    room_id: str | None = None
    state: Literal["running", "stopped", "unknown"]
    started_at: datetime
    stopped_at: datetime | None = None
    start_mode: str | None = None
    last_sequence: int = Field(ge=1)
    event_count: int = Field(ge=1)


class LivestreamSessionPage(VersionedModel):
    sessions: tuple[LivestreamSessionSummary, ...]
    next_cursor: str | None = None
    has_more: bool = False


class LivestreamEvent(VersionedModel):
    sequence: int = Field(ge=1)
    record_id: str
    session_id: str
    event_type: str
    occurred_at: datetime
    source: str
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict[str, Any]


class LivestreamEventPage(VersionedModel):
    events: tuple[LivestreamEvent, ...]
    next_cursor: str | None = None
    has_more: bool = False


class LivestreamSpeechRequest(VersionedModel):
    text: str = Field(min_length=1, max_length=1000)


class LivestreamDanmakuRequest(VersionedModel):
    text: str = Field(min_length=1, max_length=1000)


class LivestreamCommandAccepted(StrictModel):
    command: CommandResponse


__all__ = [
    "LivestreamCommandAccepted",
    "LivestreamDanmakuRequest",
    "LivestreamEvent",
    "LivestreamEventPage",
    "LivestreamSessionPage",
    "LivestreamSessionSummary",
    "LivestreamSpeechRequest",
    "LivestreamStatus",
    "_required_timestamp",
    "_timestamp",
]
