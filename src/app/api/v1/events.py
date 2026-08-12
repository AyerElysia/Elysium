"""P3-03 授权 Life Event query 与耐久 SSE subscription。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from plugins.life_engine.service.event_bus import (
    LifeEvent,
    RawEventGapError,
)

from .auth_store import SessionRecord
from .schemas.events import (
    EventActor,
    EventEnvelope,
    EventFilter,
    EventPage,
    EventReplyTarget,
    EventSource,
    EventSubscriptionValidation,
    EventVisibility,
)
from .tokens import SignedValueCodec, SignedValueError

EVENT_CURSOR_LEDGER = "life-events-v1"
_MAX_SCAN_PER_PAGE = 10_000
_SCAN_BATCH = 500


@runtime_checkable
class LifeEventLedgerReader(Protocol):
    """只读 Life Event ledger 的最小读取面。

    查询服务需要 ``read_since`` 推进 cursor 扫描，以及 ``read_tail``
    从尾部向前定位最近事件（避免全账本线性扫描）。旧 ``RawEventStore``
    与阶段二新 ``LifeEventStorePort``（Local/MySQL）都满足该读取面，
    因此这里不使用任何具体类型判定，避免把新存储后端误判为不可用。
    """

    async def read_since(
        self,
        position: int,
        *,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        """Read events after an opaque monotonic position."""

    async def read_tail(self, limit: int = 100) -> list[LifeEvent]:
        """Read a bounded tail of the most recent events (ascending order)."""


@dataclass(frozen=True, slots=True)
class EventQueryFailure(Exception):
    """不依赖 HTTP 的事件查询失败。"""

    code: str
    message: str
    status_code: int
    retryable: bool = False
    recovery_cursor: str | None = None


class EventQueryService:
    """在权威 Life Event ledger 上执行授权过滤和 cursor 扫描。"""

    def __init__(
        self,
        *,
        node_id: str,
        codec: SignedValueCodec,
        store_provider: Callable[[], LifeEventLedgerReader | None] | None = None,
        poll_interval: float = 0.25,
        heartbeat_interval: float = 15.0,
    ) -> None:
        self._node_id = node_id
        self._codec = codec
        self._store_provider = store_provider or (lambda: None)
        self._poll_interval = max(0.01, poll_interval)
        self._heartbeat_interval = max(self._poll_interval, heartbeat_interval)

    def validate_subscription(
        self,
        event_filter: EventFilter,
        session: SessionRecord,
    ) -> EventSubscriptionValidation:
        """预检技术 filter；不创建服务器端 durable subscription。"""

        payload_authorized = self._can_request_payload(session)
        if event_filter.include_payload and not payload_authorized:
            raise EventQueryFailure(
                "forbidden",
                "当前会话无权订阅事件 payload。",
                403,
            )
        effective_projection = (
            "full"
            if event_filter.projection == "full" and event_filter.include_payload
            else "summary"
        )
        return EventSubscriptionValidation(
            valid=True,
            filter=event_filter,
            required_scopes=("events:read",),
            effective_projection=effective_projection,
            payload_authorized=payload_authorized,
        )

    async def query(
        self,
        *,
        cursor: str | None,
        limit: int,
        event_filter: EventFilter,
        session: SessionRecord,
    ) -> EventPage:
        """返回最多 ``limit`` 个可见事件，并推进已扫描权威位置。"""

        store = self._require_store()
        position = self._decode_cursor(cursor)
        start_position = position
        visible: list[EventEnvelope] = []
        scanned = 0
        exhausted = False
        while len(visible) < limit and scanned < _MAX_SCAN_PER_PAGE:
            batch_limit = min(_SCAN_BATCH, _MAX_SCAN_PER_PAGE - scanned)
            try:
                batch = await store.read_since(position, limit=batch_limit)
            except RawEventGapError as exc:
                raise self._history_gap(exc) from exc
            if not batch:
                exhausted = True
                break
            for event in batch:
                position = event.sequence
                scanned += 1
                if not self._matches(event, event_filter):
                    continue
                if not self._is_visible(event, session):
                    continue
                visible.append(self._project(event, event_filter, session))
                if len(visible) >= limit:
                    break
            if len(batch) < batch_limit:
                exhausted = True
                break

        if position == start_position and cursor is None:
            position = 0
        has_more = not exhausted
        if not has_more:
            try:
                probe = await store.read_since(position, limit=1)
            except RawEventGapError as exc:
                raise self._history_gap(exc) from exc
            has_more = bool(probe)
        return EventPage(
            events=tuple(visible),
            next_cursor=self._encode_cursor(position),
            has_more=has_more,
            scanned_count=scanned,
        )

    async def get_event(
        self,
        event_id: str,
        *,
        event_filter: EventFilter,
        session: SessionRecord,
    ) -> EventEnvelope:
        """读取单个授权事件；不存在与不可见统一返回相同失败。"""

        event = await self._require_store().get_by_event_id(event_id)
        if (
            event is None
            or not self._matches(event, event_filter)
            or not self._is_visible(event, session)
        ):
            raise EventQueryFailure(
                "resource_not_found",
                "请求的事件不存在。",
                404,
            )
        return self._project(event, event_filter, session)

    async def stream(
        self,
        *,
        cursor: str | None,
        event_filter: EventFilter,
        session: SessionRecord,
    ) -> AsyncIterator[str]:
        """从 cursor 补历史后持续 tail；heartbeat 不推进业务 cursor。"""

        self.validate_subscription(event_filter, session)
        position = self._decode_cursor(cursor)
        last_emission = asyncio.get_running_loop().time()
        while True:
            page = await self.query(
                cursor=self._encode_cursor(position),
                limit=500,
                event_filter=event_filter,
                session=session,
            )
            next_position = self._decode_cursor(page.next_cursor)
            for visible_index, event in enumerate(page.events, start=1):
                event_position = event.sequence
                if visible_index == len(page.events):
                    event_position = next_position
                yield self._sse_event(
                    event="life_event",
                    cursor=self._encode_cursor(event_position),
                    data=event.model_dump(mode="json"),
                )
                last_emission = asyncio.get_running_loop().time()
            position = next_position
            # heartbeat 独立于 has_more：即使尾部持续有新事件导致
            # has_more 恒为 True（query 只在读到空页/耗尽扫描预算时才为 False），
            # 只要超过 heartbeat 间隔没有 yield 事件，就必须发出心跳注释帧，
            # 否则长连接的读超时会误判断线（见运行观察：chat.message 过滤下
            # 尾部 heartbeat 事件每 60s 引发一次 ReadTimeout）。
            elapsed = asyncio.get_running_loop().time() - last_emission
            if elapsed >= self._heartbeat_interval:
                yield ": heartbeat\n\n"
                last_emission = asyncio.get_running_loop().time()
            if page.has_more:
                await asyncio.sleep(0)
                continue
            await asyncio.sleep(self._poll_interval)

    def error_frame(self, failure: EventQueryFailure) -> str:
        """将流建立后的 gap 等失败编码为结构化 SSE 错误。"""

        payload: dict[str, Any] = {
            "error": {
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
            }
        }
        if failure.recovery_cursor:
            payload["error"]["recovery"] = {
                "action": "restart_from_cursor",
                "cursor": failure.recovery_cursor,
            }
        return self._sse_event(event="error", cursor=None, data=payload)

    def _require_store(self) -> LifeEventLedgerReader:
        store = self._store_provider()
        if store is None:
            raise EventQueryFailure(
                "component_unavailable",
                "Life Event ledger 当前不可用。",
                503,
                retryable=True,
            )
        return store

    def _decode_cursor(self, cursor: str | None) -> int:
        if cursor is None or not cursor.strip():
            return 0
        try:
            return self._codec.decode_cursor(cursor, ledger=EVENT_CURSOR_LEDGER)
        except SignedValueError as exc:
            raise EventQueryFailure(
                "cursor_invalid",
                "事件 cursor 无效。",
                422,
            ) from exc

    def _encode_cursor(self, position: int) -> str:
        return self._codec.encode_cursor(position, ledger=EVENT_CURSOR_LEDGER)

    def _history_gap(self, exc: RawEventGapError) -> EventQueryFailure:
        safe_position = max(0, exc.earliest_available - 1)
        return EventQueryFailure(
            "history_gap",
            "请求的事件历史已不连续。",
            409,
            recovery_cursor=self._encode_cursor(safe_position),
        )

    @staticmethod
    def _metadata(event: LifeEvent) -> Mapping[str, Any]:
        return event.metadata if isinstance(event.metadata, Mapping) else {}

    @classmethod
    def _visibility(cls, event: LifeEvent) -> tuple[str, tuple[str, ...]]:
        raw = cls._metadata(event).get("visibility", "private")
        if isinstance(raw, Mapping):
            scope = str(raw.get("scope") or "private").strip().lower()
            audience_raw = raw.get("audience", ())
            audience = (
                tuple(str(item) for item in audience_raw)
                if isinstance(audience_raw, (list, tuple))
                else ()
            )
            return scope, audience
        return str(raw or "private").strip().lower(), ()

    @classmethod
    def _is_visible(cls, event: LifeEvent, session: SessionRecord) -> bool:
        if session.role == "administrator":
            return True
        scope, audience = cls._visibility(event)
        if scope == "public":
            return True
        metadata = cls._metadata(event)
        actor_id = str(metadata.get("actor_id") or metadata.get("user_id") or "")
        if actor_id and actor_id == session.actor_id:
            return True
        grants = set(session.resource_grants)
        candidates = {
            "*",
            "events:*",
            f"event:{event.event_id}",
            f"stream:{event.stream_id}" if event.stream_id else "",
            (
                f"consciousness:{event.source_instance_id}"
                if event.source_instance_id
                else ""
            ),
        }
        candidates.update(f"audience:{item}" for item in audience)
        return bool(grants.intersection(candidates - {""}))

    @staticmethod
    def _can_request_payload(session: SessionRecord) -> bool:
        if session.role == "administrator":
            return True
        grants = set(session.resource_grants)
        return session.role == "platform_service" or bool(
            grants.intersection({"*", "events:*", "events:payload"})
        )

    @classmethod
    def _matches(cls, event: LifeEvent, event_filter: EventFilter) -> bool:
        if event_filter.event_type and not any(
            event.event_type == prefix or event.event_type.startswith(f"{prefix}.")
            for prefix in event_filter.event_type
        ):
            return False
        if event_filter.channel and event.channel not in event_filter.channel:
            return False
        if event_filter.stream_id and event.stream_id != event_filter.stream_id:
            return False
        if (
            event_filter.source_instance_id
            and event.source_instance_id != event_filter.source_instance_id
        ):
            return False
        occurred_at = cls._parse_time(event.timestamp)
        if event_filter.occurred_after and occurred_at <= event_filter.occurred_after:
            return False
        return not (
            event_filter.occurred_before and occurred_at >= event_filter.occurred_before
        )

    def _project(
        self,
        event: LifeEvent,
        event_filter: EventFilter,
        session: SessionRecord,
    ) -> EventEnvelope:
        metadata = self._metadata(event)
        visibility_scope, audience = self._visibility(event)
        occurred_at = self._parse_time(event.timestamp)
        recorded_at = self._parse_time(event.recorded_at or event.timestamp)
        payload = self._payload(event)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        include_payload = (
            event_filter.projection == "full"
            and event_filter.include_payload
            and self._can_request_payload(session)
        )
        reply_target = self._reply_target(event.reply_target)
        actor_id = str(
            metadata.get("actor_id")
            or metadata.get("user_id")
            or event.source
            or "unknown"
        )
        return EventEnvelope(
            event_id=event.event_id,
            sequence=event.sequence,
            origin_node_id=str(metadata.get("origin_node_id") or self._node_id),
            origin_sequence=max(0, int(event.source_sequence or 0)),
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            published_at=None,
            actor=EventActor(
                type=str(metadata.get("actor_type") or "component"),
                id=actor_id,
                display_name=(
                    str(metadata["actor_display_name"])
                    if metadata.get("actor_display_name")
                    else None
                ),
            ),
            source=EventSource(
                component=event.source or "unknown",
                connection=(
                    str(metadata["source_connection"])
                    if metadata.get("source_connection")
                    else None
                ),
            ),
            channel=event.channel,
            event_type=event.event_type,
            consciousness_instance_id=event.source_instance_id or None,
            stream_id=event.stream_id or None,
            reply_target=reply_target,
            correlation_id=event.correlation_id or None,
            causation_id=event.causation_id or None,
            visibility=EventVisibility(scope=visibility_scope, audience=audience),
            payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
            payload=payload if include_payload else None,
            detail_url=f"/api/v1/events/{event.event_id}",
        )

    @staticmethod
    def _payload(event: LifeEvent) -> dict[str, Any]:
        return {
            "content": event.content,
            "content_ref": event.content_ref or None,
            "priority": int(event.priority),
            "salience": float(event.salience),
            "metadata": dict(event.metadata),
        }

    @staticmethod
    def _reply_target(raw: dict[str, Any] | None) -> EventReplyTarget | None:
        if not raw:
            return None
        target_id = str(raw.get("id") or raw.get("stream_id") or "").strip()
        if not target_id:
            return None
        return EventReplyTarget(
            type=str(raw.get("type") or "stream"),
            id=target_id,
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Life Event timestamp must be timezone-aware")
        return parsed.astimezone(UTC)

    @staticmethod
    def _sse_event(
        *,
        event: str,
        cursor: str | None,
        data: Mapping[str, Any],
    ) -> str:
        lines = [f"event: {event}"]
        if cursor is not None:
            lines.append(f"id: {cursor}")
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"data: {encoded}")
        return "\n".join(lines) + "\n\n"


def event_store_from_bot(bot: Any) -> LifeEventLedgerReader | None:
    """只读取已初始化的 Life Engine ledger，不触发懒创建。

    判定基于最小读取面（``read_since``），而不是某个具体类型：旧
    ``RawEventStore`` 与阶段二新 ``LifeEventStorePort``（Local/MySQL）都能
    通过，避免 MySQL 共享多写者部署下把可用账本误判为不可用。
    """

    manager = getattr(bot, "plugin_manager", None)
    get_all = getattr(manager, "get_all_plugins", None)
    if not callable(get_all):
        return None
    plugin = get_all().get("life_engine")
    service = getattr(plugin, "_service", None)
    event_bus = getattr(service, "_event_bus", None)
    store = getattr(event_bus, "store", None)
    if callable(getattr(store, "read_since", None)):
        return store
    return None


__all__ = [
    "EVENT_CURSOR_LEDGER",
    "EventQueryFailure",
    "EventQueryService",
    "event_store_from_bot",
]
