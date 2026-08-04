"""Durable chat command facade, provider capabilities, and HTTP routes."""

# FastAPI dependencies are intentionally declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Response

from src.core.models.media import MediaAttachment
from src.core.models.message import Message, MessageType
from src.kernel.commands import (
    CommandDispatcher,
    CommandOutcome,
    CommandRecord,
    CommandStatus,
    CommandStore,
    HandlerRegistry,
    IdempotencyConflict,
)

from .auth_store import SessionRecord
from .chat import ChatTargetResolutionFailure
from .commands import IDEMPOTENCY_KEY_PATTERN, _response
from .runtime import ERROR_RESPONSES, APIError
from .schemas.chat_commands import (
    ChatAnnouncementRequest,
    ChatCommandAccepted,
    ChatEditRequest,
    ChatForwardRequest,
    ChatPokeRequest,
    ChatReactionRequest,
    ChatReplyRequest,
    ChatSendRequest,
    MessagePart,
)


class CapabilityError(RuntimeError):
    """A provider does not expose the requested native operation."""


class DeliveryUnknownError(RuntimeError):
    """The provider may have accepted an operation but did not confirm it."""


class ChatAction(StrEnum):
    SEND = "chat.message.send"
    REPLY = "chat.message.reply"
    EDIT = "chat.message.edit"
    RECALL = "chat.message.recall"
    REACTION_ADD = "chat.reaction.add"
    REACTION_REMOVE = "chat.reaction.remove"
    MARK_READ = "chat.message.mark_read"
    FORWARD = "chat.message.forward"
    POKE = "chat.poke.send"
    ANNOUNCEMENT_PUBLISH = "chat.announcement.publish"
    ANNOUNCEMENT_DELETE = "chat.announcement.delete"
    PIN = "chat.message.pin"
    UNPIN = "chat.message.unpin"


USER_SCOPES: Mapping[ChatAction, frozenset[str]] = {
    ChatAction.SEND: frozenset({"chat:write"}),
    ChatAction.REPLY: frozenset({"chat:write"}),
    ChatAction.EDIT: frozenset({"chat:write"}),
    ChatAction.RECALL: frozenset({"chat:write"}),
    ChatAction.REACTION_ADD: frozenset({"chat:write"}),
    ChatAction.REACTION_REMOVE: frozenset({"chat:write"}),
    ChatAction.MARK_READ: frozenset({"chat:write"}),
    ChatAction.FORWARD: frozenset({"chat:write"}),
    ChatAction.POKE: frozenset({"chat:write"}),
    ChatAction.ANNOUNCEMENT_PUBLISH: frozenset({"chat:admin", "chat:moderate"}),
    ChatAction.ANNOUNCEMENT_DELETE: frozenset({"chat:admin", "chat:moderate"}),
    ChatAction.PIN: frozenset({"chat:admin", "chat:moderate"}),
    ChatAction.UNPIN: frozenset({"chat:admin", "chat:moderate"}),
}


@dataclass(frozen=True, slots=True)
class ChatTarget:
    """Resolved public target plus opaque provider identities from P3-05."""

    stream_id: str
    platform: str
    chat_type: str
    adapter_signature: str | None = None
    provider_message_id: str | None = None
    provider_target: Mapping[str, Any] = field(default_factory=dict)
    message_direction: str | None = None
    message_actor_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_target", dict(self.provider_target))


class ChatTargetResolver(Protocol):
    async def resolve_stream(
        self,
        stream_id: str,
        actor_id: str,
        authorization: Mapping[str, Any],
    ) -> ChatTarget: ...

    async def resolve_message(
        self,
        message_id: str,
        actor_id: str,
        authorization: Mapping[str, Any],
    ) -> ChatTarget: ...


class ManagedMediaResolver(Protocol):
    """P3-07 boundary: only ready managed objects can cross into transport."""

    async def resolve_ready(
        self,
        media_id: str,
        *,
        actor_id: str,
        expected_type: str,
    ) -> MediaAttachment: ...


class MessageSenderProtocol(Protocol):
    async def send_message(
        self,
        message: Message,
        adapter_signature: str | None = None,
    ) -> bool: ...


class PlatformChatFacade(Protocol):
    """Domain facade hiding Feishu/NapCat private clients from API handlers."""

    platform: str

    def capabilities(self) -> Mapping[ChatAction, bool]: ...

    async def perform(
        self,
        action: ChatAction,
        *,
        target: ChatTarget,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any] | None: ...


@dataclass(slots=True)
class ProviderFacadeRegistry:
    """Explicit platform allowlist; no generic private-client passthrough exists."""

    facades: Mapping[str, PlatformChatFacade]

    def get(self, platform: str) -> PlatformChatFacade:
        facade = self.facades.get(platform)
        if facade is None:
            raise CapabilityError(f"platform {platform!r} is not exported")
        return facade


class ChatCommandService:
    """Execute validated chat commands through stable domain boundaries."""

    def __init__(
        self,
        *,
        sender: MessageSenderProtocol,
        targets: ChatTargetResolver,
        media: ManagedMediaResolver | None,
        providers: ProviderFacadeRegistry,
    ) -> None:
        self.sender = sender
        self.targets = targets
        self.media = media
        self.providers = providers

    def register(self, registry: HandlerRegistry) -> None:
        for action, scopes in USER_SCOPES.items():
            registry.register(
                action.value,
                self.handle,
                required_scopes=scopes,
                timeout_seconds=30.0,
            )

    async def handle(self, command: CommandRecord) -> CommandOutcome:
        action = ChatAction(command.command_type)
        try:
            if action in {ChatAction.SEND, ChatAction.REPLY}:
                return await self._send(command, reply=action is ChatAction.REPLY)
            return await self._provider_action(command, action)
        except CapabilityError as exc:
            return CommandOutcome(
                status=CommandStatus.REJECTED,
                error_code="capability_disabled",
                safe_error_detail=str(exc),
            )
        except DeliveryUnknownError:
            return CommandOutcome(
                status=CommandStatus.DELIVERY_UNKNOWN,
                error_code="delivery_unknown",
                safe_error_detail="平台可能已执行操作，但未返回可靠确认。",
            )
        except ChatTargetResolutionFailure:
            return CommandOutcome(
                status=CommandStatus.REJECTED,
                error_code="resource_not_found",
                safe_error_detail="聊天目标不存在或当前命令无权访问。",
            )
        except RuntimeError:
            return CommandOutcome(
                status=CommandStatus.FAILED,
                error_code="provider_failed",
                safe_error_detail="平台确认聊天操作失败。",
            )
        except (KeyError, TypeError, ValueError):
            return CommandOutcome(
                status=CommandStatus.FAILED,
                error_code="validation_failed",
                safe_error_detail="聊天命令内容不符合领域协议。",
            )

    async def _send(self, command: CommandRecord, *, reply: bool) -> CommandOutcome:
        message_id = str(command.target.get("message_id") or "")
        authorization = self._authorization(command)
        reply_to: str | None = None
        if reply:
            target = await self.targets.resolve_message(
                message_id,
                command.actor_id,
                authorization,
            )
            if not target.provider_message_id:
                raise CapabilityError("reply target has no provider identity")
            reply_to = target.provider_message_id
        else:
            target = await self.targets.resolve_stream(
                str(command.target["stream_id"]),
                command.actor_id,
                authorization,
            )
            public_reply_to = command.payload.get("reply_to")
            if public_reply_to:
                source = await self.targets.resolve_message(
                    str(public_reply_to),
                    command.actor_id,
                    authorization,
                )
                if source.stream_id != target.stream_id:
                    raise ChatTargetResolutionFailure("reply_stream_mismatch")
                if not source.provider_message_id:
                    raise CapabilityError("reply target has no provider identity")
                reply_to = source.provider_message_id
        parts = _parts_from_payload(command.payload)
        attachments = await self._resolve_attachments(parts, command.actor_id)
        text = "".join(part.text or "" for part in parts if part.type == "text")
        primary = next((part.type for part in parts if part.type != "text"), "text")
        message = Message(
            message_id=str(
                command.payload.get("client_message_id") or f"api_{uuid4().hex}"
            ),
            stream_id=target.stream_id,
            reply_to=reply_to,
            content=text or f"[{primary}]",
            processed_plain_text=text or None,
            message_type=MessageType(primary),
            platform=target.platform,
            chat_type=target.chat_type,
            attachments=attachments,
            extra={
                "api_command_id": command.command_id,
                "api_actor_id": command.actor_id,
            },
        )
        succeeded = await self.sender.send_message(message, target.adapter_signature)
        if not succeeded:
            if message.extra.get("delivery_status") == "unknown":
                raise DeliveryUnknownError
            return CommandOutcome(
                status=CommandStatus.FAILED,
                error_code="delivery_failed",
                safe_error_detail="平台确认消息发送失败。",
            )
        return CommandOutcome(
            status=CommandStatus.SUCCEEDED,
            result={"message_id": message.message_id, "stream_id": target.stream_id},
        )

    async def _resolve_attachments(
        self,
        parts: Sequence[MessagePart],
        actor_id: str,
    ) -> list[MediaAttachment]:
        media_parts = [part for part in parts if part.type != "text"]
        if media_parts and self.media is None:
            raise CapabilityError("managed media resolver is unavailable")
        attachments: list[MediaAttachment] = []
        for part in media_parts:
            assert self.media is not None and part.media_id is not None
            attachment = await self.media.resolve_ready(
                part.media_id,
                actor_id=actor_id,
                expected_type=part.type,
            )
            attachments.append(attachment)
        return attachments

    async def _provider_action(
        self,
        command: CommandRecord,
        action: ChatAction,
    ) -> CommandOutcome:
        message_id = str(command.target.get("message_id") or "")
        authorization = self._authorization(command)
        if message_id:
            target = await self.targets.resolve_message(
                message_id,
                command.actor_id,
                authorization,
            )
            if action in {ChatAction.EDIT, ChatAction.RECALL}:
                self._require_owned_message(target, command.actor_id)
        else:
            target = await self.targets.resolve_stream(
                str(command.target["stream_id"]),
                command.actor_id,
                authorization,
            )
        payload: Mapping[str, Any] = command.payload
        if action is ChatAction.FORWARD:
            payload = await self._forward_payload(
                command,
                authorization,
                target.platform,
            )
        facade = self.providers.get(target.platform)
        if not facade.capabilities().get(action, False):
            raise CapabilityError(
                f"provider {target.platform!r} does not support {action.value!r}"
            )
        result = await facade.perform(action, target=target, payload=payload)
        return CommandOutcome(status=CommandStatus.SUCCEEDED, result=dict(result or {}))

    async def _forward_payload(
        self,
        command: CommandRecord,
        authorization: Mapping[str, Any],
        destination_platform: str,
    ) -> Mapping[str, Any]:
        message_ids = command.payload.get("message_ids")
        if not isinstance(message_ids, list):
            raise TypeError("message_ids must be a list")
        provider_ids: list[str] = []
        for message_id in message_ids:
            source = await self.targets.resolve_message(
                str(message_id),
                command.actor_id,
                authorization,
            )
            if source.platform != destination_platform:
                raise CapabilityError("cross-provider forward is not supported")
            if not source.provider_message_id:
                raise CapabilityError("source message has no provider identity")
            provider_ids.append(source.provider_message_id)
        return {**command.payload, "message_ids": provider_ids}

    @staticmethod
    def _require_owned_message(target: ChatTarget, actor_id: str) -> None:
        if (
            target.message_direction != "delivered"
            or target.message_actor_id != actor_id
        ):
            raise ChatTargetResolutionFailure("message_not_owned")

    @staticmethod
    def _authorization(command: CommandRecord) -> Mapping[str, Any]:
        value = command.payload.get("_authorization")
        if not isinstance(value, Mapping):
            raise TypeError("command authorization snapshot is missing")
        return value


def _parts_from_payload(payload: Mapping[str, Any]) -> tuple[MessagePart, ...]:
    values = payload.get("parts")
    if not isinstance(values, list):
        raise TypeError("parts must be a list")
    return tuple(MessagePart.model_validate(value) for value in values)


def create_chat_command_router(
    *,
    store: CommandStore,
    dispatcher: CommandDispatcher,
    require_scope: Callable[..., Callable[[SessionRecord], SessionRecord]],
) -> APIRouter:
    """Create public domain routes backed by the durable command ledger."""

    router = APIRouter()

    async def accept(
        *,
        action: ChatAction,
        target: dict[str, Any],
        payload: dict[str, Any],
        session: SessionRecord,
        key: str | None,
        response: Response,
    ) -> ChatCommandAccepted:
        normalized_key = (key or "").strip()
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized_key):
            raise APIError(
                "idempotency_key_required",
                "该命令需要有效的 Idempotency-Key。",
                status_code=422,
            )
        request_hash = store.request_hash(
            command_type=action.value,
            schema_version=1,
            target=target,
            payload=payload,
            correlation_id=None,
            expected_revision=None,
        )
        authorization = {
            "session_id": session.session_id,
            "resource_grants": sorted(set(session.resource_grants)),
        }
        stored_payload = {**payload, "_authorization": authorization}
        try:
            command, created = await asyncio.to_thread(
                store.accept,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                command_type=action.value,
                schema_version=1,
                actor_id=session.actor_id,
                caller_role=session.role,
                scopes=session.scopes,
                target=target,
                payload=stored_payload,
            )
        except IdempotencyConflict as exc:
            raise APIError(
                "idempotency_conflict",
                "该 Idempotency-Key 已用于不同命令。",
                status_code=409,
            ) from exc
        if created:
            dispatcher.schedule(command.command_id)
        else:
            response.status_code = 200
        return ChatCommandAccepted(command=_response(command))

    async def write_session(
        session: SessionRecord = Depends(require_scope("chat:write")),
    ) -> SessionRecord:
        return session

    async def moderate_session(
        session: SessionRecord = Depends(require_scope("chat:admin", "chat:moderate")),
    ) -> SessionRecord:
        if session.role not in {"administrator", "platform_service"}:
            raise APIError(
                "role_required",
                "该聊天管理操作需要管理员或受信平台服务身份。",
                status_code=403,
            )
        return session

    @router.post(
        "/chat/messages:send",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="sendChatMessage",
        responses=ERROR_RESPONSES,
    )
    async def send_message(
        body: ChatSendRequest,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        data = body.model_dump(mode="json", exclude={"schema_version"})
        return await accept(
            action=ChatAction.SEND,
            target={"stream_id": body.stream_id},
            payload=data,
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/chat/messages/{message_id}:reply",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="replyChatMessage",
        responses=ERROR_RESPONSES,
    )
    async def reply_message(
        message_id: str,
        body: ChatReplyRequest,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.REPLY,
            target={"message_id": message_id},
            payload=body.model_dump(mode="json", exclude={"schema_version"}),
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/chat/messages/{message_id}:edit",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="editChatMessage",
        responses=ERROR_RESPONSES,
    )
    async def edit_message(
        message_id: str,
        body: ChatEditRequest,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.EDIT,
            target={"message_id": message_id},
            payload=body.model_dump(mode="json", exclude={"schema_version"}),
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/chat/messages/{message_id}:recall",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="recallChatMessage",
        responses=ERROR_RESPONSES,
    )
    async def recall_message(
        message_id: str,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.RECALL,
            target={"message_id": message_id},
            payload={},
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/chat/messages/{message_id}/reactions",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="addChatReaction",
        responses=ERROR_RESPONSES,
    )
    async def add_reaction(
        message_id: str,
        body: ChatReactionRequest,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.REACTION_ADD,
            target={"message_id": message_id},
            payload={"reaction": body.reaction},
            session=session,
            key=key,
            response=response,
        )

    @router.delete(
        "/chat/messages/{message_id}/reactions/{reaction}",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="removeChatReaction",
        responses=ERROR_RESPONSES,
    )
    async def remove_reaction(
        message_id: str,
        reaction: str,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.REACTION_REMOVE,
            target={"message_id": message_id},
            payload={"reaction": reaction},
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/chat/messages/{message_id}:mark-read",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="markChatMessageRead",
        responses=ERROR_RESPONSES,
    )
    async def mark_read(
        message_id: str,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.MARK_READ,
            target={"message_id": message_id},
            payload={},
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/chat/messages:forward",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="forwardChatMessages",
        responses=ERROR_RESPONSES,
    )
    async def forward(
        body: ChatForwardRequest,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.FORWARD,
            target={"stream_id": body.stream_id},
            payload={"message_ids": list(body.message_ids)},
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/chat/streams/{stream_id}/poke",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="pokeChatStream",
        responses=ERROR_RESPONSES,
    )
    async def poke(
        stream_id: str,
        body: ChatPokeRequest,
        response: Response,
        session: SessionRecord = Depends(write_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.POKE,
            target={"stream_id": stream_id},
            payload={"target_id": body.target_id},
            session=session,
            key=key,
            response=response,
        )

    @router.post(
        "/admin/chat/streams/{stream_id}/announcements",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="publishChatAnnouncement",
        responses=ERROR_RESPONSES,
    )
    async def announce(
        stream_id: str,
        body: ChatAnnouncementRequest,
        response: Response,
        session: SessionRecord = Depends(moderate_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.ANNOUNCEMENT_PUBLISH,
            target={"stream_id": stream_id},
            payload={"content": body.content},
            session=session,
            key=key,
            response=response,
        )

    @router.delete(
        "/admin/chat/streams/{stream_id}/announcements/{announcement_id}",
        response_model=ChatCommandAccepted,
        status_code=202,
        operation_id="deleteChatAnnouncement",
        responses=ERROR_RESPONSES,
    )
    async def delete_announcement(
        stream_id: str,
        announcement_id: str,
        response: Response,
        session: SessionRecord = Depends(moderate_session),
        key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ChatCommandAccepted:
        return await accept(
            action=ChatAction.ANNOUNCEMENT_DELETE,
            target={"stream_id": stream_id},
            payload={"announcement_id": announcement_id},
            session=session,
            key=key,
            response=response,
        )

    for suffix, action, operation in (
        (":pin", ChatAction.PIN, "pinChatMessage"),
        (":unpin", ChatAction.UNPIN, "unpinChatMessage"),
    ):

        async def pin_action(
            message_id: str,
            response: Response,
            session: SessionRecord = Depends(moderate_session),
            key: str | None = Header(default=None, alias="Idempotency-Key"),
            _action: ChatAction = action,
        ) -> ChatCommandAccepted:
            return await accept(
                action=_action,
                target={"message_id": message_id},
                payload={},
                session=session,
                key=key,
                response=response,
            )

        router.add_api_route(
            f"/admin/chat/messages/{{message_id}}{suffix}",
            pin_action,
            methods=["POST"],
            response_model=ChatCommandAccepted,
            status_code=202,
            operation_id=operation,
            responses=ERROR_RESPONSES,
        )

    return router


__all__ = [
    "CapabilityError",
    "ChatAction",
    "ChatCommandService",
    "ChatTarget",
    "ChatTargetResolver",
    "DeliveryUnknownError",
    "ManagedMediaResolver",
    "PlatformChatFacade",
    "ProviderFacadeRegistry",
    "create_chat_command_router",
]
