"""P3-06 chat command contracts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from src.app.api.v1.media_contracts import ManagedMediaFailure
from src.app.api.v1.media_objects import ManagedMediaService, MediaObjectStore
from src.app.api.v1.schemas.media import MediaUploadCreateRequest

from src.app.api.v1.chat_commands import (
    ChatAction,
    ChatCommandService,
    ChatTarget,
    ProviderFacadeRegistry,
)
from src.app.api.v1.chat_runtime import create_chat_command_service
from src.app.api.v1.chat_platforms import AylaChatFacade
from src.app.api.v1.schemas.chat_commands import MessagePart
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.kernel.commands import CommandRecord, CommandStatus, HandlerRegistry
from src.kernel.llm.payload.media import MediaKind, MediaRef


def _command(command_type: ChatAction, *, target=None, payload=None) -> CommandRecord:
    now = datetime.now(UTC)
    return CommandRecord(
        command_id="cmd-1",
        idempotency_key="idem-key-1",
        request_hash="hash",
        command_type=command_type.value,
        schema_version=1,
        actor_id="actor-1",
        caller_role="user",
        scope_snapshot=("chat:write", "chat:moderate"),
        target=target or {"stream_id": "stream-1"},
        payload={
            **(payload or {}),
            "_authorization": {
                "session_id": "session-1",
                "resource_grants": ["stream:stream-1"],
            },
        },
        status=CommandStatus.EXECUTING,
        created_at=now,
        accepted_at=now,
        started_at=now,
        finished_at=None,
        result_event_id=None,
        result=None,
        error_code=None,
        safe_error_detail=None,
        correlation_id=None,
        causation_id=None,
        expected_revision=None,
        attempt_count=1,
        cancellation_requested=False,
        task_id="task-1",
    )


class _Targets:
    async def resolve_stream(
        self,
        stream_id: str,
        actor_id: str,
        authorization,
    ) -> ChatTarget:
        assert actor_id == "actor-1" and authorization["session_id"] == "session-1"
        return ChatTarget(stream_id, "feishu", "private", "feishu:adapter")

    async def resolve_message(
        self,
        message_id: str,
        actor_id: str,
        authorization,
    ) -> ChatTarget:
        assert message_id and actor_id == "actor-1"
        assert authorization["session_id"] == "session-1"
        return ChatTarget(
            "stream-1",
            "feishu",
            "private",
            "feishu:adapter",
            provider_message_id=f"provider-{message_id}",
            message_direction="delivered",
            message_actor_id="actor-1",
        )


class _Provider:
    platform = "feishu"

    def __init__(self, supported: set[ChatAction]) -> None:
        self.supported = supported
        self.perform = AsyncMock(return_value={"provider_receipt": "receipt-1"})

    def capabilities(self):
        return {action: action in self.supported for action in ChatAction}


@pytest.mark.parametrize("field", ["url", "path", "base64", "data"])
def test_public_message_part_rejects_source_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        MessagePart.model_validate(
            {"type": "image", "media_id": "media-1", field: "secret"}
        )


def test_message_parts_require_text_or_managed_media_id() -> None:
    assert MessagePart(type="text", text="hello").text == "hello"
    assert MessagePart(type="voice", media_id="media-1").media_id == "media-1"
    with pytest.raises(ValidationError):
        MessagePart(type="image")
    with pytest.raises(ValidationError):
        MessagePart(type="text", text="hello", media_id="media-1")


def test_all_chat_handlers_are_explicitly_registered() -> None:
    service = ChatCommandService(
        sender=AsyncMock(),
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({}),
    )
    registry = HandlerRegistry()
    service.register(registry)
    assert set(registry.command_types) == {action.value for action in ChatAction}


@pytest.mark.asyncio
async def test_text_send_maps_to_message_sender() -> None:
    sender = SimpleNamespace(send_message=AsyncMock(return_value=True))
    service = ChatCommandService(
        sender=sender,
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({}),
    )
    outcome = await service.handle(
        _command(
            ChatAction.SEND,
            payload={
                "parts": [{"type": "text", "text": "hello"}],
                "client_message_id": "client-1",
            },
        )
    )
    assert outcome.status is CommandStatus.SUCCEEDED
    message = sender.send_message.await_args.args[0]
    assert message.content == "hello"
    assert message.stream_id == "stream-1"
    assert message.extra["api_actor_id"] == "actor-1"
    assert sender.send_message.await_args.args[1] == "feishu:adapter"


@pytest.mark.asyncio
async def test_reply_and_send_reply_to_use_provider_message_identity() -> None:
    sender = SimpleNamespace(send_message=AsyncMock(return_value=True))
    service = ChatCommandService(
        sender=sender,
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({}),
    )
    reply = await service.handle(
        _command(
            ChatAction.REPLY,
            target={"message_id": "public-1"},
            payload={"parts": [{"type": "text", "text": "reply"}]},
        )
    )
    assert reply.status is CommandStatus.SUCCEEDED
    assert sender.send_message.await_args.args[0].reply_to == "provider-public-1"

    send = await service.handle(
        _command(
            ChatAction.SEND,
            payload={
                "reply_to": "public-2",
                "parts": [{"type": "text", "text": "reply"}],
            },
        )
    )
    assert send.status is CommandStatus.SUCCEEDED
    assert sender.send_message.await_args.args[0].reply_to == "provider-public-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("part_type", "segment_type", "kind", "data"),
    [
        (
            "image",
            MediaSegmentType.IMAGE,
            MediaKind.IMAGE,
            b"\x89PNG\r\n\x1a\n" + b"0" * 32,
        ),
        (
            "voice",
            MediaSegmentType.VOICE,
            MediaKind.AUDIO,
            b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"0" * 32,
        ),
    ],
)
async def test_managed_media_is_resolved_before_send(
    part_type, segment_type, kind, data
) -> None:
    attachment = MediaAttachment(segment_type, MediaRef.from_bytes(data, kind=kind))
    media = SimpleNamespace(resolve_ready=AsyncMock(return_value=attachment))
    sender = SimpleNamespace(send_message=AsyncMock(return_value=True))
    service = ChatCommandService(
        sender=sender,
        targets=_Targets(),
        media=media,
        providers=ProviderFacadeRegistry({}),
    )
    outcome = await service.handle(
        _command(
            ChatAction.SEND,
            payload={"parts": [{"type": part_type, "media_id": "media-1"}]},
        )
    )
    assert outcome.status is CommandStatus.SUCCEEDED
    media.resolve_ready.assert_awaited_once_with(
        "media-1",
        actor_id="actor-1",
        expected_type=part_type,
        resource_grants=("stream:stream-1",),
    )
    assert sender.send_message.await_args.args[0].attachments == [attachment]


@pytest.mark.asyncio
async def test_real_managed_media_store_resolves_grant_bound_chat_attachment(
    tmp_path,
) -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"managed-chat-image"
    store = MediaObjectStore(
        tmp_path / "api.sqlite3",
        tmp_path / "runtime" / "media",
    )
    try:
        upload = store.create_upload(
            MediaUploadCreateRequest(
                schema_version=1,
                kind="image",
                mime_type="image/png",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                file_name="chat.png",
                resource_grant="stream:stream-1",
            ),
            actor_id="owner",
            grants=("stream:stream-1",),
        )
        store.put_upload(upload.upload_id, data, actor_id="owner")
        descriptor = store.complete_upload(upload.upload_id, actor_id="owner")
        sender = SimpleNamespace(send_message=AsyncMock(return_value=True))
        service = ChatCommandService(
            sender=sender,
            targets=_Targets(),
            media=ManagedMediaService(store),
            providers=ProviderFacadeRegistry({}),
        )
        outcome = await service.handle(
            _command(
                ChatAction.SEND,
                payload={
                    "parts": [
                        {"type": "image", "media_id": descriptor.media_id}
                    ]
                },
            )
        )
        assert outcome.status is CommandStatus.SUCCEEDED
        attachment = sender.send_message.await_args.args[0].attachments[0]
        assert attachment.resource_id == descriptor.media_id
        assert attachment.media_ref.data == data
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "error_code"),
    [
        (
            ManagedMediaFailure("media_not_found", status_code=404),
            CommandStatus.REJECTED,
            "resource_not_found",
        ),
        (
            ManagedMediaFailure("media_type_mismatch", status_code=422),
            CommandStatus.FAILED,
            "validation_failed",
        ),
        (
            ManagedMediaFailure("media_integrity_failed", status_code=409),
            CommandStatus.FAILED,
            "media_failed",
        ),
    ],
)
async def test_managed_media_failures_keep_stable_command_semantics(
    failure, status, error_code
) -> None:
    media = SimpleNamespace(resolve_ready=AsyncMock(side_effect=failure))
    service = ChatCommandService(
        sender=SimpleNamespace(send_message=AsyncMock(return_value=True)),
        targets=_Targets(),
        media=media,
        providers=ProviderFacadeRegistry({}),
    )
    outcome = await service.handle(
        _command(
            ChatAction.SEND,
            payload={"parts": [{"type": "image", "media_id": "media-1"}]},
        )
    )
    assert outcome.status is status
    assert outcome.error_code == error_code


@pytest.mark.asyncio
async def test_media_without_p3_07_resolver_is_capability_rejection() -> None:
    service = ChatCommandService(
        sender=SimpleNamespace(send_message=AsyncMock(return_value=True)),
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({}),
    )
    outcome = await service.handle(
        _command(
            ChatAction.SEND,
            payload={"parts": [{"type": "image", "media_id": "media-1"}]},
        )
    )
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.error_code == "capability_disabled"


@pytest.mark.asyncio
async def test_sender_delivery_results_are_observable() -> None:
    message_payload = {"parts": [{"type": "text", "text": "hello"}]}
    sender = SimpleNamespace(send_message=AsyncMock(return_value=False))
    service = ChatCommandService(
        sender=sender,
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({}),
    )
    failed = await service.handle(_command(ChatAction.SEND, payload=message_payload))
    assert failed.status is CommandStatus.FAILED
    assert failed.error_code == "delivery_failed"

    async def unknown(message, _adapter):
        message.extra["delivery_status"] = "unknown"
        return False

    sender.send_message.side_effect = unknown
    unknown_outcome = await service.handle(
        _command(ChatAction.SEND, payload=message_payload)
    )
    assert unknown_outcome.status is CommandStatus.DELIVERY_UNKNOWN
    assert unknown_outcome.error_code == "delivery_unknown"


@pytest.mark.asyncio
async def test_provider_capability_never_degrades_to_text() -> None:
    provider = _Provider(set())
    sender = SimpleNamespace(send_message=AsyncMock(return_value=True))
    service = ChatCommandService(
        sender=sender,
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({"feishu": provider}),
    )
    outcome = await service.handle(
        _command(ChatAction.POKE, payload={"target_id": "user-2"})
    )
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.error_code == "capability_disabled"
    sender.send_message.assert_not_awaited()
    provider.perform.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [ChatAction.POKE, ChatAction.ANNOUNCEMENT_PUBLISH])
async def test_supported_provider_matrix_is_executable(action: ChatAction) -> None:
    provider = _Provider({action})
    service = ChatCommandService(
        sender=SimpleNamespace(send_message=AsyncMock()),
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({"feishu": provider}),
    )
    outcome = await service.handle(_command(action, payload={"content": "notice"}))
    assert outcome.status is CommandStatus.SUCCEEDED
    assert outcome.result == {"provider_receipt": "receipt-1"}


@pytest.mark.asyncio
async def test_forward_resolves_public_message_ids_to_provider_ids() -> None:
    provider = _Provider({ChatAction.FORWARD})
    service = ChatCommandService(
        sender=SimpleNamespace(send_message=AsyncMock()),
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({"feishu": provider}),
    )
    outcome = await service.handle(
        _command(
            ChatAction.FORWARD,
            payload={"message_ids": ["public-1"]},
        )
    )
    assert outcome.status is CommandStatus.SUCCEEDED
    assert provider.perform.await_args.kwargs["payload"]["message_ids"] == [
        "provider-public-1"
    ]


@pytest.mark.asyncio
async def test_cross_provider_forward_is_rejected_without_provider_call() -> None:
    class CrossProviderTargets(_Targets):
        async def resolve_message(self, message_id, actor_id, authorization):
            return ChatTarget(
                "qq:group:100",
                "qq",
                "group",
                "qq:adapter",
                provider_message_id="9001",
            )

    provider = _Provider({ChatAction.FORWARD})
    service = ChatCommandService(
        sender=SimpleNamespace(send_message=AsyncMock()),
        targets=CrossProviderTargets(),
        media=None,
        providers=ProviderFacadeRegistry({"feishu": provider}),
    )
    outcome = await service.handle(
        _command(
            ChatAction.FORWARD,
            payload={"message_ids": ["public-1"]},
        )
    )
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.error_code == "capability_disabled"
    provider.perform.assert_not_awaited()


@pytest.mark.asyncio
async def test_ayla_provider_action_is_capability_disabled() -> None:
    """Ayla 命令操作由应用内处理；Elysium 命令端点以 capability_disabled 拒绝。"""

    class AylaTargets(_Targets):
        async def resolve_message(self, message_id, actor_id, authorization):
            return ChatTarget(
                "stream-ayla",
                "ayla",
                "private",
                "ayla_adapter:adapter:ayla_adapter",
                provider_message_id="provider-1",
                message_direction="delivered",
                message_actor_id="actor-1",
            )

    service = create_chat_command_service(
        AylaTargets(),
        message_sender=SimpleNamespace(send_message=AsyncMock()),
    )
    outcome = await service.handle(
        _command(
            ChatAction.RECALL,
            target={"message_id": "public-1"},
        )
    )
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.error_code == "capability_disabled"


def test_ayla_facade_capabilities_are_all_disabled() -> None:
    facade = AylaChatFacade()
    assert facade.platform == "ayla"
    assert all(not supported for supported in facade.capabilities().values())
    for action in ChatAction:
        assert facade.capabilities()[action] is False

@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["feishu", "qq"])
async def test_late_bound_unloaded_adapter_is_capability_disabled(
    platform: str,
) -> None:
    class PlatformTargets(_Targets):
        async def resolve_message(self, message_id, actor_id, authorization):
            if platform == "qq":
                return ChatTarget(
                    "qq:group:100",
                    "qq",
                    "group",
                    "qq:adapter",
                    provider_message_id="9001",
                    provider_target={"group_id": "100"},
                )
            return await super().resolve_message(message_id, actor_id, authorization)

    class EmptyManager:
        @staticmethod
        def get_all_adapters():
            return {}

    service = create_chat_command_service(
        PlatformTargets(),
        message_sender=SimpleNamespace(send_message=AsyncMock()),
        feishu_provider=lambda: None,
        adapter_manager_provider=lambda: EmptyManager(),
    )
    outcome = await service.handle(
        _command(
            ChatAction.REACTION_ADD,
            target={"message_id": "public-1"},
            payload={"reaction": "THUMBSUP" if platform == "feishu" else "128077"},
        )
    )
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.error_code == "capability_disabled"


@pytest.mark.asyncio
async def test_edit_and_recall_require_actor_owned_delivered_message() -> None:
    class ReceivedTargets(_Targets):
        async def resolve_message(self, message_id, actor_id, authorization):
            return ChatTarget(
                "stream-1",
                "feishu",
                "private",
                "feishu:adapter",
                provider_message_id="provider-1",
                message_direction="received",
                message_actor_id="other",
            )

    provider = _Provider({ChatAction.EDIT, ChatAction.RECALL})
    service = ChatCommandService(
        sender=SimpleNamespace(send_message=AsyncMock()),
        targets=ReceivedTargets(),
        media=None,
        providers=ProviderFacadeRegistry({"feishu": provider}),
    )
    for action in (ChatAction.EDIT, ChatAction.RECALL):
        outcome = await service.handle(
            _command(
                action,
                target={"message_id": "public-1"},
                payload={"parts": [{"type": "text", "text": "edit"}]},
            )
        )
        assert outcome.status is CommandStatus.REJECTED
        assert outcome.error_code == "resource_not_found"
    provider.perform.assert_not_awaited()
