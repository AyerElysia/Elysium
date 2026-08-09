"""P3-05 authorized chat history API contracts."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plugins.life_engine.service.event_bus import LifeEvent, RawEventStore
from src.app.api.v1.auth_store import AuthStore, SessionRecord
from src.app.api.v1.chat import (
    ChatQueryFailure,
    ChatQueryService,
    ChatTargetResolutionFailure,
    LedgerChatTargetResolver,
)
from src.app.api.v1.policy import PLATFORM_SERVICE_AUDIENCE
from src.app.api.v1.runtime import APIContext, create_api_app
from src.app.api.v1.tokens import SignedValueCodec


def _session(
    *,
    actor_id: str = "reader",
    role: str = "platform_service",
    grants: tuple[str, ...] = (),
) -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        session_id=f"session-{actor_id}",
        actor_id=actor_id,
        audience=PLATFORM_SERVICE_AUDIENCE,
        role=role,
        scopes=("chat:read",),
        resource_grants=grants,
        access_expires_at=now + timedelta(minutes=5),
        refresh_expires_at=now + timedelta(hours=1),
    )


def _chat_event(
    event_id: str,
    *,
    message_id: str,
    stream_id: str,
    provider: str = "feishu",
    chat_type: str = "private",
    event_type: str = "chat.message.received",
    actor_id: str = "sender-1",
    reply_to: str | None = None,
    attachments: list[dict] | None = None,
    provider_receipt: dict | None = None,
    provider_identity: dict | None = None,
) -> LifeEvent:
    direction = (
        "delivered" if event_type.startswith("chat.message.delivery") else "received"
    )
    metadata = {
        "actor_id": actor_id,
        "visibility": {"scope": "private", "audience": []},
        "chat": {
            "message_id": message_id,
            "stream_id": stream_id,
            "direction": direction,
            "platform": provider,
            "chat_type": chat_type,
            "message_type": "image" if attachments else "text",
            "sender": {"id": actor_id, "name": "Sender"},
            "reply_to": reply_to,
            "parts": [{"type": "text", "text": f"content-{message_id}"}],
            "attachments": attachments or [],
        },
        "provider_identity": provider_identity
        or {
            "provider": provider,
            "message_id": f"raw-{message_id}",
        },
    }
    if provider_receipt is not None:
        metadata["provider_receipt"] = provider_receipt
    return LifeEvent(
        event_id=event_id,
        sequence=0,
        timestamp=datetime.now(UTC).isoformat(),
        source=f"{provider}_adapter",
        channel="chat",
        event_type=event_type,
        content=f"content-{message_id}",
        stream_id=stream_id,
        source_instance_id="chat_global",
        metadata=metadata,
        occurrence_id=f"occ-{event_id}",
    )


@pytest.mark.asyncio
async def test_message_query_cursor_scans_hidden_events_without_leaking(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    hidden = await store.append(
        _chat_event(
            "hidden",
            message_id="hidden-message",
            stream_id="feishu:private:hidden",
        )
    )
    visible = await store.append(
        _chat_event(
            "visible",
            message_id="visible-message",
            stream_id="feishu:private:visible",
        )
    )
    codec = SignedValueCodec("c" * 48)
    service = ChatQueryService(codec=codec, store_provider=lambda: store)
    reader = _session(grants=("stream:feishu:private:visible",))

    page = await service.query_messages(
        stream_id=None,
        cursor=None,
        limit=10,
        session=reader,
    )

    assert [item.message_id for item in page.messages] == ["visible-message"]
    assert page.scanned_count == 2
    assert not page.has_more
    assert (
        codec.decode_cursor(page.next_cursor, ledger="chat-events-v1")
        == visible.sequence
    )
    assert hidden.event_id not in page.model_dump_json()


@pytest.mark.asyncio
async def test_private_group_reply_and_media_descriptor_authorization(
    tmp_path: Path,
) -> None:
    descriptor = {
        "segment_type": "image",
        "media_ref": {
            "kind": "image",
            "mime_type": "image/png",
            "sha256": "a" * 64,
            "size_bytes": 128,
            "source_message_id": "raw-image-1",
        },
        "metadata": {"filename": "picture.png", "resource_id": "resource-1"},
    }
    store = RawEventStore(tmp_path / "life")
    await store.append(
        _chat_event(
            "private",
            message_id="private-1",
            stream_id="feishu:private:user-1",
            reply_to="private-parent",
            attachments=[descriptor],
        )
    )
    await store.append(
        _chat_event(
            "group",
            message_id="group-1",
            stream_id="qq:group:100",
            provider="qq",
            chat_type="group",
        )
    )
    service = ChatQueryService(
        codec=SignedValueCodec("d" * 48),
        store_provider=lambda: store,
    )

    private_reader = _session(grants=("stream:feishu:private:user-1",))
    private_page = await service.query_messages(
        stream_id="feishu:private:user-1",
        cursor=None,
        limit=10,
        session=private_reader,
    )
    message = private_page.messages[0]
    assert message.reply_to == "private-parent"
    assert message.attachments[0]["segment_type"] == "image"
    assert message.attachments[0]["media_ref"]["sha256"] == "a" * 64
    assert message.attachments[0]["metadata"] == descriptor["metadata"]
    assert "base64" not in message.model_dump_json().lower()
    assert "path" not in message.model_dump_json().lower()

    with pytest.raises(ChatQueryFailure) as hidden_group:
        await service.get_message("group-1", private_reader)
    assert hidden_group.value.status_code == 404


@pytest.mark.asyncio
async def test_receipt_requires_visible_message_and_keeps_provider_receipt(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    await store.append(
        _chat_event(
            "delivered",
            message_id="outbound-1",
            stream_id="feishu:private:user-1",
            event_type="chat.message.delivery_confirmed",
            actor_id="bot",
            provider_receipt={"message_id": "om-provider-1"},
        )
    )
    service = ChatQueryService(
        codec=SignedValueCodec("r" * 48),
        store_provider=lambda: store,
    )
    reader = _session(grants=("stream:feishu:private:user-1",))

    receipts = await service.get_receipts("outbound-1", reader)

    assert len(receipts.receipts) == 1
    assert receipts.receipts[0].status == "confirmed"
    assert receipts.receipts[0].provider_receipt == {"message_id": "om-provider-1"}


@pytest.mark.asyncio
async def test_duplicate_message_id_requires_resource_disambiguation(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    await store.append(
        _chat_event(
            "duplicate-feishu",
            message_id="same-id",
            stream_id="feishu:private:user-1",
        )
    )
    await store.append(
        _chat_event(
            "duplicate-qq",
            message_id="same-id",
            stream_id="qq:group:100",
            provider="qq",
            chat_type="group",
        )
    )
    service = ChatQueryService(
        codec=SignedValueCodec("a" * 48),
        store_provider=lambda: store,
    )
    admin = _session(role="administrator")

    with pytest.raises(ChatQueryFailure) as ambiguous:
        await service.get_message("same-id", admin)
    assert ambiguous.value.code == "resource_ambiguous"

    selected = await service.get_message(
        "same-id",
        admin,
        provider="qq",
        stream_id="qq:group:100",
    )
    assert selected.provider == "qq"


@pytest.mark.asyncio
async def test_command_target_resolver_uses_current_session_and_provider_identity(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    await store.append(
        _chat_event(
            "target",
            message_id="message-1",
            stream_id="qq:group:100",
            provider="qq",
            chat_type="group",
            actor_id="actor-1",
            provider_identity={
                "provider": "qq",
                "adapter_signature": "qq:adapter",
                "raw_message_id": "9001",
                "group_id": "100",
            },
        )
    )
    auth = AuthStore(tmp_path / "auth.sqlite3", installation_id="chat-target")
    codec = SignedValueCodec("t" * 48)
    credential_id = auth.add_credential(
        actor_id="actor-1",
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret="chat-target-service-secret-long-enough",
        scopes=("chat:read", "chat:write"),
        resource_grants=("stream:qq:group:100",),
    )
    session, _, _ = auth.issue_session_from_credential(
        credential="chat-target-service-secret-long-enough",
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    resolver = LedgerChatTargetResolver(
        queries=ChatQueryService(codec=codec, store_provider=lambda: store),
        auth_store=auth,
    )
    authorization = {
        "session_id": session.session_id,
        "resource_grants": list(session.resource_grants),
    }
    try:
        target = await resolver.resolve_message("message-1", "actor-1", authorization)
        assert target.adapter_signature == "qq:adapter"
        assert target.provider_message_id == "9001"
        assert target.provider_target == {"group_id": "100"}
        assert target.message_direction == "received"
        assert target.message_actor_id == "actor-1"

        auth.revoke_session(session.session_id)
        with pytest.raises(ChatTargetResolutionFailure):
            await resolver.resolve_message("message-1", "actor-1", authorization)

        replacement, _, _ = auth.issue_session_from_credential(
            credential="chat-target-service-secret-long-enough",
            audience=PLATFORM_SERVICE_AUDIENCE,
            codec=codec,
            access_ttl=timedelta(minutes=5),
            refresh_ttl=timedelta(hours=1),
        )
        replacement_authorization = {
            "session_id": replacement.session_id,
            "resource_grants": list(replacement.resource_grants),
        }
        auth.revoke_credential(credential_id)
        with pytest.raises(ChatTargetResolutionFailure):
            await resolver.resolve_message(
                "message-1",
                "actor-1",
                replacement_authorization,
            )
    finally:
        auth.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", ["expired", "grants_reduced"])
async def test_command_target_resolver_rejects_expired_or_reduced_authorization(
    tmp_path: Path,
    invalid_state: str,
) -> None:
    store = RawEventStore(tmp_path / "life")
    await store.append(
        _chat_event(
            "authorization-target",
            message_id="message-1",
            stream_id="qq:group:100",
            provider="qq",
            chat_type="group",
            actor_id="actor-1",
            provider_identity={
                "provider": "qq",
                "adapter_signature": "qq:adapter",
                "raw_message_id": "9001",
                "group_id": "100",
            },
        )
    )
    auth = AuthStore(tmp_path / "auth.sqlite3", installation_id="chat-target")
    codec = SignedValueCodec("v" * 48)
    auth.add_credential(
        actor_id="actor-1",
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret="authorization-state-secret-long-enough",
        scopes=("chat:read", "chat:write"),
        resource_grants=("stream:qq:group:100",),
    )
    session, _, _ = auth.issue_session_from_credential(
        credential="authorization-state-secret-long-enough",
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    authorization = {
        "session_id": session.session_id,
        "resource_grants": list(session.resource_grants),
    }
    if invalid_state == "expired":
        auth._connection.execute(
            "UPDATE api_sessions SET access_expires_at = ? WHERE session_id = ?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                session.session_id,
            ),
        )
    else:
        auth._connection.execute(
            "UPDATE api_sessions SET resource_grants_json = ? WHERE session_id = ?",
            ("[]", session.session_id),
        )
    auth._connection.commit()
    resolver = LedgerChatTargetResolver(
        queries=ChatQueryService(codec=codec, store_provider=lambda: store),
        auth_store=auth,
    )
    try:
        with pytest.raises(ChatTargetResolutionFailure):
            await resolver.resolve_message("message-1", "actor-1", authorization)
    finally:
        auth.close()


@pytest.mark.asyncio
async def test_command_target_resolver_hides_ambiguous_and_hidden_resources(
    tmp_path: Path,
) -> None:
    store = RawEventStore(tmp_path / "life")
    for provider, stream_id in (
        ("feishu", "feishu:private:one"),
        ("qq", "qq:group:100"),
    ):
        await store.append(
            _chat_event(
                f"duplicate-{provider}",
                message_id="duplicate",
                stream_id=stream_id,
                provider=provider,
                chat_type="group" if provider == "qq" else "private",
            )
        )
    auth = AuthStore(tmp_path / "auth.sqlite3", installation_id="chat-target")
    codec = SignedValueCodec("u" * 48)
    auth.add_credential(
        actor_id="reader",
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret="chat-target-reader-secret-long-enough",
        scopes=("chat:read", "chat:write"),
        resource_grants=("chat:*",),
    )
    session, _, _ = auth.issue_session_from_credential(
        credential="chat-target-reader-secret-long-enough",
        audience=PLATFORM_SERVICE_AUDIENCE,
        codec=codec,
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(hours=1),
    )
    resolver = LedgerChatTargetResolver(
        queries=ChatQueryService(codec=codec, store_provider=lambda: store),
        auth_store=auth,
    )
    authorization = {
        "session_id": session.session_id,
        "resource_grants": list(session.resource_grants),
    }
    try:
        with pytest.raises(ChatTargetResolutionFailure):
            await resolver.resolve_message("duplicate", "reader", authorization)
        with pytest.raises(ChatTargetResolutionFailure):
            await resolver.resolve_stream("missing", "reader", authorization)
    finally:
        auth.close()


def test_chat_http_scope_resource_hiding_and_openapi(tmp_path: Path) -> None:
    store = RawEventStore(tmp_path / "life")
    import asyncio

    asyncio.run(
        store.append(
            _chat_event(
                "http-message",
                message_id="http-1",
                stream_id="feishu:private:http",
            )
        )
    )
    auth = AuthStore(tmp_path / "auth.sqlite3", installation_id="chat-api")
    codec = SignedValueCodec("h" * 48)
    context = APIContext(
        store=auth,
        codec=codec,
        installation_id="chat-api",
        chat=ChatQueryService(codec=codec, store_provider=lambda: store),
    )
    secret = "chat-api-service-secret-long-enough"
    auth.add_credential(
        actor_id="http-reader",
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret=secret,
        scopes=("chat:read",),
        resource_grants=(),
    )
    client = TestClient(create_api_app(context))
    session = client.post(
        "/auth/sessions",
        json={
            "grant_type": "service_credential",
            "audience": PLATFORM_SERVICE_AUDIENCE,
            "service_credential": secret,
        },
    )
    headers = {"Authorization": f"Bearer {session.json()['access_token']}"}
    try:
        hidden = client.get("/chat/messages/http-1", headers=headers)
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "resource_not_found"

        openapi = client.get("/openapi.json").json()
        operations = {
            operation["operationId"]
            for path in openapi["paths"].values()
            for operation in path.values()
            if isinstance(operation, dict) and "operationId" in operation
        }
        assert {
            "queryChatStreams",
            "getChatStream",
            "queryChatMessages",
            "getChatMessage",
            "getChatMessageReceipts",
        }.issubset(operations)
    finally:
        auth.close()


def _legacy_text_event(
    sequence: int,
    *,
    event_id: str,
    stream_id: str,
    content: str,
    source: str = "feishu",
    chat_type: str = "private",
    sender: str = "独立应用",
    sender_id: str = "api_user",
) -> LifeEvent:
    """构造旧通道 ``text``/``channel=chat`` 归一化事件（同 inject 消息形态）。"""
    return LifeEvent(
        event_id=event_id,
        sequence=sequence,
        timestamp="2026-08-09T13:00:00+00:00",
        source=source,
        channel="chat",
        event_type="text",
        content=content,
        stream_id=stream_id,
        reply_target={
            "stream_id": stream_id,
            "source": source,
            "chat_type": chat_type,
            "sender": sender,
            "sender_id": sender_id,
        },
        source_instance_id="chat_global",
        occurrence_id=f"occ-{event_id}",
        metadata={
            "actor_id": sender_id,
            "visibility": {"scope": "private", "audience": []},
            "chat_type": chat_type,
            "sender": sender,
            "sender_id": sender_id,
            "content_type": "text",
            "legacy_event_type": "message",
        },
    )


@pytest.mark.asyncio
async def test_legacy_text_message_projectable_through_tail(tmp_path: Path) -> None:
    """旧通道 text 事件能经尾部优先投影为聊天消息，供独立应用查询。"""
    store = RawEventStore(tmp_path / "life")
    await store.append(
        _chat_event(
            "modern",
            message_id="modern-msg",
            stream_id="feishu:private:modern",
            provider="feishu",
        )
    )
    await store.append(
        _legacy_text_event(
            2,
            event_id="inject_abc123",
            stream_id="6a994480bc44405a7f0311bc37b25b33a5dffdaf1a47de679cf630271d840a65",
            content="来自独立应用的测试消息",
            source="feishu",
        )
    )
    codec = SignedValueCodec("c" * 48)
    service = ChatQueryService(codec=codec, store_provider=lambda: store)
    reader = _session(actor_id="reader", grants=("*",))

    # 无 cursor：尾部优先应同时捞出 chat.message.* 与 text 事件
    page = await service.query_messages(
        stream_id=None,
        cursor=None,
        limit=10,
        session=reader,
    )
    message_ids = {item.message_id for item in page.messages}
    assert "modern-msg" in message_ids
    assert "inject_abc123" in message_ids

    # 按流定位：get_stream 尾部优先命中 text 事件投影的流
    stream = await service.get_stream(
        "6a994480bc44405a7f0311bc37b25b33a5dffdaf1a47de679cf630271d840a65",
        reader,
    )
    assert stream.stream_id == (
        "6a994480bc44405a7f0311bc37b25b33a5dffdaf1a47de679cf630271d840a65"
    )
    assert stream.provider == "feishu"
    assert stream.last_message_text == "来自独立应用的测试消息"

    # 按消息定位：get_message 尾部优先命中 text 事件消息
    message = await service.get_message("inject_abc123", reader)
    assert message.provider == "feishu"
    assert message.chat_type == "private"
    assert message.direction == "received"
    assert message.parts[0].text == "来自独立应用的测试消息"


@pytest.mark.asyncio
async def test_find_stream_target_tail_first_uses_text_projection(
    tmp_path: Path,
) -> None:
    """find_stream_target 尾部优先，text 事件用其投影的 provider_identity 兜底。"""
    store = RawEventStore(tmp_path / "life")
    # 先写一个无关旧事件，再写 text 聊天事件，确认不依赖从头扫描
    await store.append(
        _chat_event(
            "early",
            message_id="early-msg",
            stream_id="feishu:private:other",
            provider="feishu",
        )
    )
    await store.append(
        _legacy_text_event(
            2,
            event_id="inject_xyz789",
            stream_id="feishu:private:target",
            content="嗨",
            source="feishu",
        )
    )
    codec = SignedValueCodec("c" * 48)
    service = ChatQueryService(codec=codec, store_provider=lambda: store)
    reader = _session(actor_id="reader", grants=("*",))

    summary, identity = await service.find_stream_target(
        "feishu:private:target", reader
    )
    assert summary.stream_id == "feishu:private:target"
    assert summary.provider == "feishu"
    # text 事件无 metadata.provider_identity，用投影兜底（含 provider）
    assert identity.get("provider") == "feishu"
