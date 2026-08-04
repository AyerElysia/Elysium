"""Strict public schemas for durable chat commands."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .commands import CommandResponse
from .common import VersionedModel

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
MessagePartType = Literal["text", "image", "voice", "video", "file", "emoji"]


class MessagePart(VersionedModel):
    """One normalized message part; media is referenced only by managed id."""

    type: MessagePartType
    text: str | None = Field(default=None, min_length=1, max_length=100_000)
    media_id: Identifier | None = None
    alt: str | None = Field(default=None, max_length=1_000)
    file_name: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_shape(self) -> MessagePart:
        if self.type == "text":
            if self.text is None or self.media_id is not None:
                raise ValueError("text part requires text and forbids media_id")
        elif self.media_id is None or self.text is not None:
            raise ValueError("media part requires media_id and forbids text")
        if self.file_name is not None and self.type != "file":
            raise ValueError("file_name is valid only for file parts")
        return self


class ChatSendRequest(VersionedModel):
    """Send normalized parts to one authorized stream."""

    stream_id: Identifier
    reply_to: Identifier | None = None
    parts: tuple[MessagePart, ...] = Field(min_length=1, max_length=32)
    client_message_id: Identifier | None = None


class ChatReplyRequest(VersionedModel):
    """Reply to a visible message in its existing stream."""

    parts: tuple[MessagePart, ...] = Field(min_length=1, max_length=32)
    client_message_id: Identifier | None = None


class ChatEditRequest(VersionedModel):
    """Replace editable message content."""

    parts: tuple[MessagePart, ...] = Field(min_length=1, max_length=32)


class ChatReactionRequest(VersionedModel):
    """Add one provider-supported reaction."""

    reaction: Identifier


class ChatForwardRequest(VersionedModel):
    """Forward visible messages to one authorized stream."""

    stream_id: Identifier
    message_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)


class ChatPokeRequest(VersionedModel):
    """Send a native poke without text fallback."""

    target_id: Identifier


class ChatAnnouncementRequest(VersionedModel):
    """Publish a provider-native announcement."""

    content: str = Field(min_length=1, max_length=20_000)


class ChatCommandAccepted(VersionedModel):
    """Durable acceptance response; execution result is queried by command id."""

    command: CommandResponse


__all__ = [
    "ChatAnnouncementRequest",
    "ChatCommandAccepted",
    "ChatEditRequest",
    "ChatForwardRequest",
    "ChatPokeRequest",
    "ChatReactionRequest",
    "ChatReplyRequest",
    "ChatSendRequest",
    "Identifier",
    "MessagePart",
]
