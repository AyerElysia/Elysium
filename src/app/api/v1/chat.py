"""Authorized chat history projections over the authoritative Life Event ledger."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .chat_commands import ChatTarget

from plugins.life_engine.service.event_bus import (
    LifeEvent,
    RawEventGapError,
)
from src.core.models.media import MediaAttachment
from src.kernel.llm.exceptions import MediaValidationError

from .auth_store import AuthStore, SessionRecord
from .events import LifeEventLedgerReader
from .schemas.chat import (
    ChatMessage,
    ChatMessagePage,
    ChatPart,
    ChatReceipt,
    ChatReceiptList,
    ChatSender,
    ChatStreamPage,
    ChatStreamSummary,
)
from .tokens import SignedValueCodec, SignedValueError

CHAT_CURSOR_LEDGER = "chat-events-v1"
_SCAN_BATCH = 500
_MAX_SCAN_PER_PAGE = 10_000
_MESSAGE_FACTS = {
    "chat.message.received",
    "chat.message.delivery_confirmed",
    # 旧通道以 event_type="text" 且 channel="chat" 归一化的聊天事件（如
    # inject 注入的消息、旧 QQ/飞书 adapter 的 text 归一化）。这类事件没有
    # ``metadata.chat`` 结构，靠顶层 stream_id/content/reply_target 投影。
    "text",
}
_RECEIPT_STATUS = {
    "chat.message.delivery_confirmed": "confirmed",
    "chat.message.delivery_failed": "failed",
    "chat.message.delivery_unknown": "unknown",
    "chat.message.read": "read",
}


@dataclass(frozen=True, slots=True)
class ChatQueryFailure(Exception):
    code: str
    message: str
    status_code: int
    retryable: bool = False
    recovery_cursor: str | None = None


class ChatTargetResolutionFailure(RuntimeError):
    """A command target cannot be resolved without disclosing hidden data."""


class ChatQueryService:
    """Query stable chat projections while preserving ledger cursor semantics."""

    def __init__(
        self,
        *,
        codec: SignedValueCodec,
        store_provider: Callable[[], LifeEventLedgerReader | None],
    ) -> None:
        self._codec = codec
        self._store_provider = store_provider

    async def query_messages(
        self,
        *,
        stream_id: str | None,
        cursor: str | None,
        limit: int,
        session: SessionRecord,
    ) -> ChatMessagePage:
        position = self._decode_cursor(cursor)
        start = position
        visible: list[ChatMessage] = []
        scanned = 0
        max_position = position
        min_position = 0
        # 首次查询（无 cursor）从尾部向前捞最近消息（聊天事件集中在尾部，
        # 一次 read_tail 即命中，避免全账本线性扫描跨远程 MySQL 超时）；
        # 带 cursor 时保持向后扫描语义推进分页。
        iterator = self._iter_tail_events() if not cursor else self._iter_all_events()
        async for event in iterator:
            scanned += 1
            position = event.sequence
            max_position = max(max_position, position)
            min_position = position if not min_position else min(min_position, position)
            if scanned > _MAX_SCAN_PER_PAGE:
                break
            if event.event_type not in _MESSAGE_FACTS:
                continue
            if stream_id and event.stream_id != stream_id:
                continue
            if not self._is_visible(event, session):
                continue
            projected = self._message(event)
            if projected is not None:
                visible.append(projected)
            if len(visible) >= limit:
                break
        has_more = min_position > 1
        return ChatMessagePage(
            messages=tuple(visible),
            next_cursor=self._encode_cursor(
                max_position if max_position != start or cursor else 0
            ),
            has_more=has_more,
            scanned_count=scanned,
        )

    async def query_streams(
        self,
        *,
        cursor: str | None,
        limit: int,
        session: SessionRecord,
    ) -> ChatStreamPage:
        position = self._decode_cursor(cursor)
        start = position
        scanned = 0
        max_position = position
        min_position = 0
        by_stream: dict[str, ChatStreamSummary] = {}
        # 首次查询（无 cursor）从尾部向前捞最近事件（聊天消息集中在尾部，
        # 一次 read_tail 即命中，避免全账本线性扫描跨远程 MySQL 超时）；
        # 带 cursor 时保持向后扫描语义推进分页。
        iterator = self._iter_tail_events() if not cursor else self._iter_all_events()
        async for event in iterator:
            scanned += 1
            position = event.sequence
            max_position = max(max_position, position)
            min_position = position if not min_position else min(min_position, position)
            if scanned > _MAX_SCAN_PER_PAGE:
                break
            message = self._message(event)
            if message is None or not self._is_visible(event, session):
                continue
            by_stream[message.stream_id] = self._stream_summary(message)
            if len(by_stream) >= limit:
                break
        streams = sorted(
            by_stream.values(), key=lambda item: item.last_active_at, reverse=True
        )
        has_more = min_position > 1
        return ChatStreamPage(
            streams=tuple(streams),
            next_cursor=self._encode_cursor(
                max_position if max_position != start or cursor else 0
            ),
            has_more=has_more,
            scanned_count=scanned,
        )

    async def get_stream(
        self, stream_id: str, session: SessionRecord
    ) -> ChatStreamSummary:
        latest: ChatMessage | None = None
        async for event in self._iter_tail_events():
            if event.stream_id != stream_id or not self._is_visible(event, session):
                continue
            message = self._message(event)
            if message is not None:
                latest = message
                break
        if latest is None:
            raise self._not_found("请求的聊天流不存在。")
        return self._stream_summary(latest)

    async def get_message(
        self,
        message_id: str,
        session: SessionRecord,
        *,
        provider: str | None = None,
        stream_id: str | None = None,
    ) -> ChatMessage:
        matches: dict[tuple[str, str], ChatMessage] = {}
        async for event in self._iter_tail_events():
            if not self._is_visible(event, session):
                continue
            projected = self._message(event)
            if projected is None or projected.message_id != message_id:
                continue
            if provider and projected.provider != provider:
                continue
            if stream_id and projected.stream_id != stream_id:
                continue
            matches[(projected.provider, projected.stream_id)] = projected
        if not matches:
            raise self._not_found("请求的消息不存在。")
        if len(matches) > 1:
            raise ChatQueryFailure(
                "resource_ambiguous",
                "消息 ID 在多个可见聊天资源中重复，请同时提供 provider 或 stream_id。",
                409,
            )
        return next(iter(matches.values()))

    async def get_receipts(
        self,
        message_id: str,
        session: SessionRecord,
        *,
        provider: str | None = None,
        stream_id: str | None = None,
    ) -> ChatReceiptList:
        message_resources: set[tuple[str, str]] = set()
        receipt_events: list[
            tuple[LifeEvent, Mapping[str, Any], Mapping[str, Any]]
        ] = []
        async for event in self._iter_tail_events():
            metadata = self._metadata(event)
            chat = metadata.get("chat")
            if not isinstance(chat, Mapping):
                continue
            if str(chat.get("message_id") or "") != message_id:
                continue
            event_provider = str(chat.get("platform") or "unknown")
            event_stream = str(chat.get("stream_id") or event.stream_id or "")
            if provider and event_provider != provider:
                continue
            if stream_id and event_stream != stream_id:
                continue
            if not self._is_visible(event, session):
                continue
            resource = (event_provider, event_stream)
            if event.event_type in _MESSAGE_FACTS:
                message_resources.add(resource)
            if event.event_type in _RECEIPT_STATUS:
                receipt_events.append((event, metadata, chat))
        if not message_resources:
            raise self._not_found("请求的消息不存在。")
        if len(message_resources) > 1:
            raise ChatQueryFailure(
                "resource_ambiguous",
                "消息 ID 在多个可见聊天资源中重复，请同时提供 provider 或 stream_id。",
                409,
            )
        selected = next(iter(message_resources))
        receipts: list[ChatReceipt] = []
        for event, metadata, chat in receipt_events:
            resource = (
                str(chat.get("platform") or "unknown"),
                str(chat.get("stream_id") or event.stream_id or ""),
            )
            if resource != selected:
                continue
            receipt = metadata.get("provider_receipt")
            receipts.append(
                ChatReceipt(
                    receipt_id=event.event_id,
                    message_id=message_id,
                    provider=resource[0],
                    status=_RECEIPT_STATUS[event.event_type],  # type: ignore[arg-type]
                    occurred_at=self._parse_time(event.timestamp),
                    provider_receipt=(
                        dict(receipt) if isinstance(receipt, Mapping) else None
                    ),
                    event_id=event.event_id,
                )
            )
        return ChatReceiptList(receipts=tuple(receipts))

    async def find_stream_target(
        self,
        stream_id: str,
        session: SessionRecord,
    ) -> tuple[ChatStreamSummary, dict[str, Any]]:
        """Resolve one visible stream plus its latest opaque provider identity."""

        selected: tuple[ChatStreamSummary, dict[str, Any]] | None = None
        async for event in self._iter_tail_events():
            if event.stream_id != stream_id or not self._is_visible(event, session):
                continue
            message = self._message(event)
            metadata = self._metadata(event)
            identity = metadata.get("provider_identity")
            if message is None:
                continue
            if isinstance(identity, Mapping) and identity:
                selected = (self._stream_summary(message), dict(identity))
                break
            # text 事件没有 metadata.provider_identity，用消息投影里的
            # provider_identity（来自 reply_target/顶层字段）兜底。
            projected_identity = message.provider_identity
            if projected_identity:
                selected = (self._stream_summary(message), dict(projected_identity))
                break
        if selected is None:
            raise self._not_found("请求的聊天流不存在。")
        return selected

    async def find_message_target(
        self,
        message_id: str,
        session: SessionRecord,
    ) -> tuple[ChatMessage, dict[str, Any], str]:
        """Resolve one visible message and reject ambiguous provider identities."""

        matches: dict[
            tuple[str, str],
            tuple[ChatMessage, dict[str, Any], str],
        ] = {}
        async for event in self._iter_tail_events():
            if not self._is_visible(event, session):
                continue
            message = self._message(event)
            if message is None or message.message_id != message_id:
                continue
            metadata = self._metadata(event)
            identity = metadata.get("provider_identity")
            if isinstance(identity, Mapping) and identity:
                matches[(message.provider, message.stream_id)] = (
                    message,
                    dict(identity),
                    str(metadata.get("actor_id") or ""),
                )
            elif message.provider_identity:
                matches[(message.provider, message.stream_id)] = (
                    message,
                    dict(message.provider_identity),
                    str(metadata.get("actor_id") or ""),
                )
        if not matches:
            raise self._not_found("请求的消息不存在。")
        if len(matches) > 1:
            raise ChatQueryFailure(
                "resource_ambiguous",
                "消息 ID 在多个可见聊天资源中重复，不能执行命令。",
                409,
            )
        return next(iter(matches.values()))

    async def _iter_all_events(self) -> AsyncIterator[LifeEvent]:
        store = self._require_store()
        position = 0
        while True:
            batch = await self._read_since(store, position, limit=_SCAN_BATCH)
            if not batch:
                return
            for event in batch:
                position = event.sequence
                yield event
            if len(batch) < _SCAN_BATCH:
                return

    async def _iter_tail_events(
        self,
        *,
        scan: int = _MAX_SCAN_PER_PAGE,
    ) -> AsyncIterator[LifeEvent]:
        """从账本尾部向前产出最近事件（新→旧）。

        查询按消息/流定位时，聊天事件总在账本尾部（旧通道 text 与
        chat.message.* 都集中在尾部），从尾部向前扫一次 ``read_tail``
        即可命中，避免 ``_iter_all_events`` 从 seq=0 全账本线性扫描
        （跨远程 MySQL 时每条 500 条分批网络往返，导致 HTTP 超时）。
        """
        store = self._require_store()
        tail = await store.read_tail(limit=max(1, int(scan)))
        for event in reversed(tail):
            yield event

    async def _read_since(
        self,
        store: LifeEventLedgerReader,
        position: int,
        *,
        limit: int,
    ) -> list[LifeEvent]:
        try:
            return await store.read_since(position, limit=limit)
        except RawEventGapError as exc:
            raise self._history_gap(exc) from exc

    def _message(self, event: LifeEvent) -> ChatMessage | None:
        if event.event_type not in _MESSAGE_FACTS:
            return None
        metadata = self._metadata(event)
        # 旧通道 text 事件没有 metadata.chat 结构：event_type=="text" 且
        # channel=="chat" 时从顶层字段 + metadata + reply_target 投影。
        if event.event_type == "text":
            return self._legacy_text_message(event, metadata)
        chat = metadata.get("chat")
        provider_identity = metadata.get("provider_identity")
        if not isinstance(chat, Mapping) or not isinstance(provider_identity, Mapping):
            return None
        message_id = str(chat.get("message_id") or "")
        stream_id = str(chat.get("stream_id") or event.stream_id or "")
        if not message_id or not stream_id:
            return None
        sender = chat.get("sender")
        sender_map = sender if isinstance(sender, Mapping) else {}
        parts = self._parts(chat.get("parts"))
        attachments = self._attachments(chat.get("attachments"))
        return ChatMessage(
            message_id=message_id,
            stream_id=stream_id,
            provider=str(chat.get("platform") or "unknown"),
            chat_type=str(chat.get("chat_type") or "unknown"),
            direction=str(chat.get("direction") or "received"),  # type: ignore[arg-type]
            message_type=str(chat.get("message_type") or "unknown"),
            sender=ChatSender(
                id=str(sender_map.get("id") or "unknown"),
                name=str(sender_map.get("name") or sender_map.get("id") or "unknown"),
                card_name=(
                    str(sender_map["card_name"])
                    if sender_map.get("card_name") is not None
                    else None
                ),
                role=(
                    str(sender_map["role"])
                    if sender_map.get("role") is not None
                    else None
                ),
            ),
            occurred_at=self._parse_time(event.timestamp),
            reply_to=str(chat["reply_to"]) if chat.get("reply_to") else None,
            parts=parts,
            attachments=attachments,
            provider_identity=dict(provider_identity),
            detail_url=f"/api/v1/chat/messages/{message_id}",
        )

    def _legacy_text_message(
        self,
        event: LifeEvent,
        metadata: Mapping[str, Any],
    ) -> ChatMessage | None:
        """投影旧通道 ``text``/``channel=chat`` 事件为聊天消息。

        这类事件来自 message_collector 归一化的入站文本（含 inject 注入），
        结构：顶层 ``stream_id``/``content``/``reply_target``，
        ``metadata.sender``/``sender_id``/``chat_type``，``source`` 为平台。
        """
        if event.channel != "chat":
            return None
        stream_id = str(event.stream_id or "").strip()
        content = str(event.content or "").strip()
        if not stream_id or not content:
            return None
        sender_id = str(metadata.get("sender_id") or "").strip()
        sender_name = str(metadata.get("sender") or "").strip()
        platform = str(event.source or "").strip() or "unknown"
        chat_type = str(metadata.get("chat_type") or "").strip() or "private"
        message_id = str(event.event_id or "").strip()
        reply_target = (
            event.reply_target if isinstance(event.reply_target, Mapping) else {}
        )
        provider_identity: dict[str, Any] = {}
        for key in (
            "adapter_signature",
            "message_id",
            "feishu_chat_id",
            "feishu_open_id",
            "open_id",
            "chat_id",
            "group_id",
            "target_user_id",
            "user_id",
            "provider",
        ):
            value = reply_target.get(key) if key in reply_target else metadata.get(key)
            if value not in {None, ""}:
                provider_identity[key] = str(value)
        if "provider" not in provider_identity:
            provider_identity["provider"] = platform
        return ChatMessage(
            message_id=message_id or f"legacy-{event.occurrence_id or event.sequence}",
            stream_id=stream_id,
            provider=platform,
            chat_type=chat_type,
            direction="received",  # type: ignore[arg-type]
            message_type="text",
            sender=ChatSender(
                id=sender_id or sender_name or "unknown",
                name=sender_name or sender_id or "unknown",
                card_name=None,
                role=None,
            ),
            occurred_at=self._parse_time(event.timestamp),
            reply_to=None,
            parts=(ChatPart(type="text", text=content),),
            attachments=(),
            provider_identity=provider_identity,
            detail_url=(f"/api/v1/chat/messages/{message_id}" if message_id else None),
        )

    @classmethod
    def _parts(cls, value: Any) -> tuple[ChatPart, ...]:
        parts: list[ChatPart] = []
        if not isinstance(value, list):
            return ()
        for raw in value:
            if not isinstance(raw, Mapping) or not raw.get("type"):
                continue
            attachment = raw.get("attachment")
            safe_attachment = cls._attachment(attachment)
            parts.append(
                ChatPart(
                    type=str(raw["type"]),
                    text=str(raw["text"]) if raw.get("text") is not None else None,
                    attachment=safe_attachment,
                )
            )
        return tuple(parts)

    @classmethod
    def _attachments(cls, value: Any) -> tuple[dict[str, Any], ...]:
        if not isinstance(value, list):
            return ()
        return tuple(
            safe for raw in value if (safe := cls._attachment(raw)) is not None
        )

    @staticmethod
    def _attachment(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        try:
            return MediaAttachment.from_descriptor(value).to_descriptor()
        except MediaValidationError:
            return None

    @staticmethod
    def _stream_summary(message: ChatMessage) -> ChatStreamSummary:
        text = next((part.text for part in message.parts if part.text), "")
        return ChatStreamSummary(
            stream_id=message.stream_id,
            provider=message.provider,
            chat_type=message.chat_type,
            last_active_at=message.occurred_at,
            last_message_id=message.message_id,
            last_message_type=message.message_type,
            last_message_text=text,
            detail_url=f"/api/v1/chat/streams/{message.stream_id}",
        )

    @classmethod
    def _is_visible(cls, event: LifeEvent, session: SessionRecord) -> bool:
        if session.role == "administrator":
            return True
        metadata = cls._metadata(event)
        actor_id = str(metadata.get("actor_id") or "")
        if actor_id and actor_id == session.actor_id:
            return True
        grants = set(session.resource_grants)
        return bool(
            grants.intersection(
                {
                    "*",
                    "chat:*",
                    f"stream:{event.stream_id}" if event.stream_id else "",
                }
            )
        )

    @staticmethod
    def _metadata(event: LifeEvent) -> Mapping[str, Any]:
        return event.metadata if isinstance(event.metadata, Mapping) else {}

    def _require_store(self) -> LifeEventLedgerReader:
        store = self._store_provider()
        if store is None:
            raise ChatQueryFailure(
                "component_unavailable",
                "聊天事件账本当前不可用。",
                503,
                retryable=True,
            )
        return store

    def _decode_cursor(self, cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            return self._codec.decode_cursor(cursor, ledger=CHAT_CURSOR_LEDGER)
        except SignedValueError as exc:
            raise ChatQueryFailure("cursor_invalid", "聊天 cursor 无效。", 422) from exc

    def _encode_cursor(self, position: int) -> str:
        return self._codec.encode_cursor(position, ledger=CHAT_CURSOR_LEDGER)

    def _history_gap(self, exc: RawEventGapError) -> ChatQueryFailure:
        return ChatQueryFailure(
            "history_gap",
            "请求的聊天历史已不连续。",
            409,
            recovery_cursor=self._encode_cursor(max(0, exc.earliest_available - 1)),
        )

    @staticmethod
    def _not_found(message: str) -> ChatQueryFailure:
        return ChatQueryFailure("resource_not_found", message, 404)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("chat event timestamp must be timezone-aware")
        return parsed.astimezone(UTC)


class LedgerChatTargetResolver:
    """Resolve command targets from P3-05 facts and current durable authorization."""

    def __init__(self, *, queries: ChatQueryService, auth_store: AuthStore) -> None:
        self._queries = queries
        self._auth_store = auth_store

    async def resolve_stream(
        self,
        stream_id: str,
        actor_id: str,
        authorization: Mapping[str, Any],
    ) -> ChatTarget:
        from .chat_commands import ChatTarget

        session = self._command_session(actor_id, authorization)
        try:
            stream, identity = await self._queries.find_stream_target(
                stream_id, session
            )
        except ChatQueryFailure as exc:
            raise ChatTargetResolutionFailure(exc.code) from exc
        return ChatTarget(
            stream_id=stream.stream_id,
            platform=stream.provider,
            chat_type=stream.chat_type,
            adapter_signature=self._text(identity.get("adapter_signature")),
            provider_target=self._provider_target(identity),
        )

    async def resolve_message(
        self,
        message_id: str,
        actor_id: str,
        authorization: Mapping[str, Any],
    ) -> ChatTarget:
        from .chat_commands import ChatTarget

        session = self._command_session(actor_id, authorization)
        try:
            (
                message,
                identity,
                message_actor_id,
            ) = await self._queries.find_message_target(
                message_id,
                session,
            )
        except ChatQueryFailure as exc:
            raise ChatTargetResolutionFailure(exc.code) from exc
        return ChatTarget(
            stream_id=message.stream_id,
            platform=message.provider,
            chat_type=message.chat_type,
            adapter_signature=self._text(identity.get("adapter_signature")),
            provider_message_id=(
                self._text(identity.get("raw_message_id"))
                or self._text(identity.get("feishu_message_id"))
                or self._text(identity.get("message_id"))
            ),
            provider_target=self._provider_target(identity),
            message_direction=message.direction,
            message_actor_id=message_actor_id,
        )

    def _command_session(
        self,
        actor_id: str,
        authorization: Mapping[str, Any],
    ) -> SessionRecord:
        session_id = authorization.get("session_id")
        grants = authorization.get("resource_grants")
        if not isinstance(session_id, str) or not isinstance(grants, list):
            raise ChatTargetResolutionFailure("authorization_invalid")
        try:
            session = self._auth_store.get_active_session(authorization["session_id"])
        except (KeyError, ValueError) as exc:
            raise ChatTargetResolutionFailure("session_invalid") from exc
        if session.actor_id != actor_id:
            raise ChatTargetResolutionFailure("session_invalid")
        snapshot = {str(item) for item in grants}
        current = set(session.resource_grants)
        if not current.issuperset(snapshot):
            raise ChatTargetResolutionFailure("authorization_reduced")
        return session

    @staticmethod
    def _provider_target(identity: Mapping[str, Any]) -> dict[str, Any]:
        target: dict[str, Any] = {}
        for source, destination in (
            ("group_id", "group_id"),
            ("target_group_id", "group_id"),
            ("user_id", "user_id"),
            ("target_user_id", "user_id"),
            ("feishu_chat_id", "chat_id"),
            ("feishu_open_id", "open_id"),
        ):
            value = identity.get(source)
            if value not in {None, ""}:
                target[destination] = value
        return target

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None


__all__ = [
    "CHAT_CURSOR_LEDGER",
    "ChatQueryFailure",
    "ChatQueryService",
    "ChatTargetResolutionFailure",
    "LedgerChatTargetResolver",
]
