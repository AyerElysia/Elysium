"""API v1 chat history projection schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .common import TimestampedModel, VersionedModel


class ChatSender(VersionedModel):
    id: str = Field(max_length=300)
    name: str = Field(max_length=300)
    card_name: str | None = Field(default=None, max_length=300)
    role: str | None = Field(default=None, max_length=100)


class ChatPart(VersionedModel):
    type: str = Field(min_length=1, max_length=100)
    text: str | None = None
    attachment: dict[str, Any] | None = None


class ChatMessage(VersionedModel, TimestampedModel):
    message_id: str = Field(min_length=1, max_length=500)
    stream_id: str = Field(min_length=1, max_length=500)
    provider: str = Field(min_length=1, max_length=100)
    chat_type: str = Field(min_length=1, max_length=100)
    direction: Literal["received", "delivered"]
    message_type: str = Field(min_length=1, max_length=100)
    sender: ChatSender
    occurred_at: datetime
    reply_to: str | None = Field(default=None, max_length=500)
    parts: tuple[ChatPart, ...]
    attachments: tuple[dict[str, Any], ...] = ()
    provider_identity: dict[str, Any]
    detail_url: str


class ChatMessagePage(VersionedModel):
    messages: tuple[ChatMessage, ...]
    next_cursor: str
    has_more: bool
    scanned_count: int = Field(ge=0)


class ChatStreamSummary(VersionedModel, TimestampedModel):
    stream_id: str = Field(min_length=1, max_length=500)
    provider: str = Field(min_length=1, max_length=100)
    chat_type: str = Field(min_length=1, max_length=100)
    last_active_at: datetime
    last_message_id: str = Field(min_length=1, max_length=500)
    last_message_type: str = Field(min_length=1, max_length=100)
    last_message_text: str = ""
    detail_url: str


class ChatStreamPage(VersionedModel):
    streams: tuple[ChatStreamSummary, ...]
    next_cursor: str
    has_more: bool
    scanned_count: int = Field(ge=0)


class ChatReceipt(VersionedModel, TimestampedModel):
    receipt_id: str = Field(min_length=1, max_length=500)
    message_id: str = Field(min_length=1, max_length=500)
    provider: str = Field(min_length=1, max_length=100)
    status: Literal["confirmed", "failed", "unknown", "read"]
    occurred_at: datetime
    provider_receipt: dict[str, Any] | None = None
    event_id: str = Field(min_length=1, max_length=500)


class ChatReceiptList(VersionedModel):
    receipts: tuple[ChatReceipt, ...]


__all__ = [
    "ChatMessage",
    "ChatMessagePage",
    "ChatPart",
    "ChatReceipt",
    "ChatReceiptList",
    "ChatSender",
    "ChatStreamPage",
    "ChatStreamSummary",
]
