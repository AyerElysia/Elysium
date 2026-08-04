"""P3-06 chat command contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from src.app.api.v1.chat_commands import (
    ChatAction,
    ChatCommandService,
    ChatTarget,
    ProviderFacadeRegistry,
)
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
        payload=payload or {},
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
    async def resolve_stream(self, stream_id: str, actor_id: str) -> ChatTarget:
        assert actor_id == "actor-1"
        return ChatTarget(stream_id, "feishu", "private", "feishu:adapter")

    async def resolve_message(self, message_id: str, actor_id: str) -> ChatTarget:
        assert message_id and actor_id == "actor-1"
        return ChatTarget("stream-1", "feishu", "private", "feishu:adapter")


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
    assert sender.send_message.await_args.args[1] == "feishu:adapter"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("part_type", "segment_type", "kind", "data"),
    [
        ("image", MediaSegmentType.IMAGE, MediaKind.IMAGE, b"\x89PNG\r\n\x1a\n" + b"0" * 32),
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
        _command(ChatAction.SEND, payload={"parts": [{"type": part_type, "media_id": "media-1"}]})
    )
    assert outcome.status is CommandStatus.SUCCEEDED
    media.resolve_ready.assert_awaited_once_with(
        "media-1", actor_id="actor-1", expected_type=part_type
    )
    assert sender.send_message.await_args.args[0].attachments == [attachment]


@pytest.mark.asyncio
async def test_media_without_p3_07_resolver_is_capability_rejection() -> None:
    service = ChatCommandService(
        sender=SimpleNamespace(send_message=AsyncMock(return_value=True)),
        targets=_Targets(),
        media=None,
        providers=ProviderFacadeRegistry({}),
    )
    outcome = await service.handle(
        _command(ChatAction.SEND, payload={"parts": [{"type": "image", "media_id": "media-1"}]})
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
    unknown_outcome = await service.handle(_command(ChatAction.SEND, payload=message_payload))
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
