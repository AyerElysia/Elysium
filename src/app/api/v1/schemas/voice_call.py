"""Stable public schemas for P3-09 realtime voice calls."""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .auth import WSTicketResponse
from .common import StrictModel, TimestampedModel, VersionedModel

VoiceCallState = Literal[
    "created",
    "connecting",
    "active",
    "interrupting",
    "stopping",
    "ended",
    "failed",
    "suspended",
]


class VoiceCallCreateRequest(VersionedModel):
    mode: Literal["auto", "full_duplex"] = "auto"


class VoiceCallTextRequest(VersionedModel):
    text: str = Field(min_length=1, max_length=8000)


class VoiceCallInterruptRequest(VersionedModel):
    played_audio_ms: int | None = Field(default=None, ge=0, le=86_400_000)


class VoiceCallTicketRequest(VersionedModel):
    role: Literal["participant", "observer"]
    origin: str | None = Field(default=None, max_length=512)


class VoiceCallStatus(TimestampedModel):
    call_id: str
    episode_id: str
    state: VoiceCallState
    mode: str = ""
    provider: str = ""
    created_at: datetime
    updated_at: datetime
    resumable: bool = False
    connected: bool = False
    input_audio_bytes: int = 0
    output_audio_bytes: int = 0
    interruptions: int = 0
    failure_reason: str = ""


class VoiceCallCreated(StrictModel):
    call: VoiceCallStatus
    connection: WSTicketResponse


class VoiceCallCommandAccepted(StrictModel):
    command: Any


class VoiceTranscriptEntry(TimestampedModel):
    sequence: int
    occurred_at: datetime
    role: Literal["user", "assistant"]
    text: str
    provider_event_id: str = ""
    visibility: Literal["participants"] = "participants"


class VoiceTranscriptPage(StrictModel):
    transcripts: tuple[VoiceTranscriptEntry, ...]
    next_cursor: str | None = None
    has_more: bool = False


__all__ = [
    "VoiceCallCommandAccepted",
    "VoiceCallCreateRequest",
    "VoiceCallCreated",
    "VoiceCallInterruptRequest",
    "VoiceCallStatus",
    "VoiceCallTextRequest",
    "VoiceCallTicketRequest",
    "VoiceTranscriptEntry",
    "VoiceTranscriptPage",
]
