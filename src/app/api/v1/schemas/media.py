"""Strict public schemas for managed media objects."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .common import TimestampedModel, VersionedModel

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]
MediaKindValue = Literal["image", "audio", "video", "file"]
MediaStateValue = Literal["ready", "saved", "quarantined"]
RecognitionStateValue = Literal["not_requested", "completed", "failed"]


class MediaUploadCreateRequest(VersionedModel):
    """Declare the immutable properties of one controlled upload."""

    kind: MediaKindValue
    mime_type: str = Field(min_length=3, max_length=127)
    size_bytes: int = Field(ge=1, le=32 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_name: str | None = Field(default=None, min_length=1, max_length=255)
    resource_grant: str | None = Field(default=None, min_length=1, max_length=240)


class MediaUploadSession(VersionedModel, TimestampedModel):
    """Safe upload session descriptor without a local path."""

    upload_id: str
    state: Literal["created", "uploaded", "completed", "failed", "expired"]
    kind: MediaKindValue
    mime_type: str
    size_bytes: int
    sha256: str
    expires_at: datetime


class MediaObjectDescriptor(VersionedModel, TimestampedModel):
    """Public media metadata; storage paths and bytes are never exported."""

    media_id: str
    state: MediaStateValue
    kind: MediaKindValue
    mime_type: str
    size_bytes: int
    sha256: str
    file_name: str | None = None
    created_at: datetime
    updated_at: datetime
    recognition_state: RecognitionStateValue


class MediaSaveResponse(VersionedModel):
    """Idempotent managed-save result."""

    media: MediaObjectDescriptor
    saved: bool


class MediaRecognizeRequest(VersionedModel):
    """Recognition options for a ready managed object."""

    use_cache: bool = True


class MediaRecognition(VersionedModel, TimestampedModel):
    """A durable recognition derivative."""

    media_id: str
    state: RecognitionStateValue
    text: str | None = None
    updated_at: datetime


class MediaDerivative(VersionedModel, TimestampedModel):
    """One safe derived resource."""

    derivative_id: str
    kind: Literal["recognition"]
    state: RecognitionStateValue
    text: str | None = None
    updated_at: datetime


class MediaDerivativeList(VersionedModel):
    """Current derived resources for one media object."""

    items: tuple[MediaDerivative, ...]


__all__ = [
    "MediaDerivative",
    "MediaDerivativeList",
    "MediaObjectDescriptor",
    "MediaRecognition",
    "MediaRecognizeRequest",
    "MediaSaveResponse",
    "MediaUploadCreateRequest",
    "MediaUploadSession",
]
