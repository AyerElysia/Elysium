"""API v1 Life Event 历史查询与订阅 schema。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .common import TimestampedModel, VersionedModel

EventProjection = Literal["summary", "full"]


class EventActor(VersionedModel):
    """事件中可安全导出的 actor 投影。"""

    type: str = Field(min_length=1, max_length=100)
    id: str = Field(min_length=1, max_length=300)
    display_name: str | None = Field(default=None, max_length=300)


class EventSource(VersionedModel):
    """事件来源组件与可选连接身份。"""

    component: str = Field(min_length=1, max_length=200)
    connection: str | None = Field(default=None, max_length=300)


class EventVisibility(VersionedModel):
    """事件资源授权投影。"""

    scope: str = Field(min_length=1, max_length=100)
    audience: tuple[str, ...] = ()


class EventReplyTarget(VersionedModel):
    """脱敏后的回复目标。"""

    type: str = Field(min_length=1, max_length=100)
    id: str = Field(min_length=1, max_length=500)


class EventEnvelope(VersionedModel, TimestampedModel):
    """基于权威 Life Event ledger 的公共事件 envelope。"""

    event_id: str = Field(min_length=1, max_length=500)
    sequence: int = Field(ge=1)
    origin_node_id: str = Field(min_length=1, max_length=200)
    origin_sequence: int = Field(ge=0)
    occurred_at: datetime
    recorded_at: datetime
    published_at: datetime | None = None
    actor: EventActor
    source: EventSource
    channel: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=300)
    consciousness_instance_id: str | None = Field(default=None, max_length=300)
    stream_id: str | None = Field(default=None, max_length=500)
    reply_target: EventReplyTarget | None = None
    correlation_id: str | None = Field(default=None, max_length=500)
    causation_id: str | None = Field(default=None, max_length=500)
    visibility: EventVisibility
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    payload: dict[str, Any] | None = None
    detail_url: str = Field(min_length=1, max_length=1000)


class EventPage(VersionedModel):
    """授权过滤后的 cursor 历史分页。"""

    events: tuple[EventEnvelope, ...]
    next_cursor: str
    has_more: bool
    scanned_count: int = Field(ge=0)


class EventFilter(VersionedModel, TimestampedModel):
    """历史与实时订阅共用的技术过滤条件。"""

    event_type: tuple[str, ...] = Field(default=(), max_length=50)
    channel: tuple[str, ...] = Field(default=(), max_length=20)
    stream_id: str | None = Field(default=None, max_length=500)
    source_instance_id: str | None = Field(default=None, max_length=300)
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None
    include_payload: bool = False
    projection: EventProjection = "summary"

    @field_validator("event_type", "channel")
    @classmethod
    def validate_filters(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item or len(item) > 300:
                raise ValueError("event filter values must be non-empty and bounded")
            normalized.append(item)
        return tuple(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_range(self) -> "EventFilter":
        if (
            self.occurred_after is not None
            and self.occurred_before is not None
            and self.occurred_after >= self.occurred_before
        ):
            raise ValueError("occurred_after must precede occurred_before")
        return self


class EventSubscriptionValidateRequest(EventFilter):
    """不创建耐久订阅的 filter 与权限预检。"""


class EventSubscriptionValidation(VersionedModel):
    """订阅预检结果及规范化过滤条件。"""

    valid: bool
    filter: EventFilter
    required_scopes: tuple[str, ...]
    effective_projection: EventProjection
    payload_authorized: bool
    transport: Literal["sse"] = "sse"


__all__ = [
    "EventActor",
    "EventEnvelope",
    "EventFilter",
    "EventPage",
    "EventProjection",
    "EventReplyTarget",
    "EventSource",
    "EventSubscriptionValidateRequest",
    "EventSubscriptionValidation",
    "EventVisibility",
]
