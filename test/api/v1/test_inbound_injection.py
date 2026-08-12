"""入站注入契约：POST /chat/messages:inject 走标准主链。

独立应用收到用户消息后，调用 ``/chat/messages:inject`` 把消息交给 Elysium
标准接收管线（``ON_MESSAGE_RECEIVED`` → Distributor → Chatter），触发爱莉
思考，而不依赖任何平台 Adapter。本文件锁定：
- schema 严格校验（stream_id/content 必填、未知字段拒绝）；
- 注入器解析 stream 的 platform/chat_type 并发布 ON_MESSAGE_RECEIVED；
- 不存在的 stream 返回 404；
- 账本不可用时返回 503，而不是凭空伪造注入目标。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from plugins.life_engine.service.event_bus import LifeEvent, RawEventStore
from src.app.api.v1.auth_store import AuthStore, SessionRecord
from src.app.api.v1.chat import ChatQueryService
from src.app.api.v1.inbound_messages import InboundInjector
from src.app.api.v1.policy import PLATFORM_SERVICE_AUDIENCE
from src.app.api.v1.runtime import APIContext, APIError, create_api_app
from src.app.api.v1.schemas.inbound_message import InboundMessageInjectRequest
from src.app.api.v1.tokens import SignedValueCodec


def _session(
    *,
    actor_id: str = "app-owner",
    role: str = "platform_service",
    scopes: tuple[str, ...] = ("chat:read", "chat:write"),
    grants: tuple[str, ...] = ("*",),
) -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        session_id=f"session-{actor_id}",
        actor_id=actor_id,
        audience=PLATFORM_SERVICE_AUDIENCE,
        role=role,
        scopes=scopes,
        resource_grants=grants,
        access_expires_at=now + timedelta(minutes=5),
        refresh_expires_at=now + timedelta(hours=1),
    )


def _chat_event(stream_id: str, sequence: int) -> LifeEvent:
    message_id = f"msg-{sequence}"
    return LifeEvent(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        timestamp="2026-08-09T12:00:00+00:00",
        source="test",
        channel="chat",
        event_type="chat.message.received",
        content="hi",
        stream_id=stream_id,
        source_instance_id="chat_global",
        metadata={
            "actor_id": "other",
            "visibility": {"scope": "private", "audience": []},
            "chat": {
                "message_id": message_id,
                "stream_id": stream_id,
                "direction": "received",
                "platform": "feishu",
                "chat_type": "private",
                "message_type": "text",
                "sender": {"id": "other", "name": "Other"},
                "parts": [{"type": "text", "text": "hi"}],
            },
            "provider_identity": {"provider": "feishu"},
        },
        occurrence_id=f"occ-{sequence}",
    )


class _PublishingStore(RawEventStore):
    """RawEventStore + 记录已发布事件的注入测试面。"""

    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self.published: list[dict[str, Any]] = []


def _build_app(tmp_path: Path) -> tuple[TestClient, _PublishingStore]:
    store = _PublishingStore(tmp_path / "events.jsonl")
    asyncio.run(store.append(_chat_event("stream-inject", 1)))
    asyncio.run(store.append(_chat_event("stream-inject", 2)))

    def store_provider() -> RawEventStore:
        return store

    codec = SignedValueCodec("s" * 48)
    queries = ChatQueryService(codec=codec, store_provider=store_provider)
    injector = InboundInjector(queries=queries)

    auth = AuthStore(tmp_path / "auth.db", installation_id="test-install")
    auth.add_credential(
        actor_id="app-owner",
        audience=PLATFORM_SERVICE_AUDIENCE,
        role="platform_service",
        secret="test-inject-secret-long-enough",
        scopes=("chat:read", "chat:write"),
        resource_grants=("*",),
    )

    context = APIContext(
        store=auth,
        codec=codec,
        installation_id="test-install",
        events=None,
        chat=queries,
        inbound_injector=injector,
        chat_commands_enabled=False,
    )
    app = create_api_app(context)
    return TestClient(app), store


@pytest.fixture
def publishing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[TestClient, _PublishingStore]:
    client, store = _build_app(tmp_path)
    published: list[dict[str, Any]] = []

    class _FakeMessageReceiver:
        async def receive_message(
            self,
            message: Any,
            adapter_signature: str,
            *,
            envelope: Any = None,
        ) -> bool:
            published.append(
                {
                    "event": "on_message_received",
                    "message": message,
                    "adapter_signature": adapter_signature,
                    "envelope": envelope,
                }
            )
            return True

    monkeypatch.setattr(
        "src.app.api.v1.inbound_messages.get_message_receiver",
        lambda: _FakeMessageReceiver(),
    )
    store.published = published
    return client, store


def _inject_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stream_id": "stream-inject",
        "content": "你好，我是独立应用发来的消息",
    }
    payload.update(overrides)
    return payload


def _headers(client: TestClient) -> dict[str, str]:
    session = client.post(
        "/auth/sessions",
        json={
            "grant_type": "service_credential",
            "audience": PLATFORM_SERVICE_AUDIENCE,
            "service_credential": "test-inject-secret-long-enough",
        },
    )
    assert session.status_code == 200, session.text
    return {"Authorization": f"Bearer {session.json()['access_token']}"}


class TestInboundInjection:
    def test_inject_publishes_message_through_standard_pipeline(
        self, publishing: tuple[TestClient, _PublishingStore]
    ) -> None:
        client, store = publishing
        response = client.post(
            "/chat/messages:inject",
            json=_inject_payload(),
            headers=_headers(client),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["accepted"] is True
        assert body["stream_id"] == "stream-inject"
        assert body["message_id"].startswith("inject_")

        assert len(store.published) == 1
        record = store.published[0]
        assert record["event"] == "on_message_received"
        message = record["message"]
        assert message.content == "你好，我是独立应用发来的消息"
        assert message.stream_id == "stream-inject"
        assert message.platform == "feishu"  # 来自账本投影，而非凭空猜测
        assert message.chat_type == "private"
        assert record["adapter_signature"] == "api-inject"
        assert record["envelope"] == {}

    def test_inject_with_sender_metadata(
        self, publishing: tuple[TestClient, _PublishingStore]
    ) -> None:
        client, store = publishing
        response = client.post(
            "/chat/messages:inject",
            json=_inject_payload(
                sender_name="外部应用",
                sender_id="app-user-1",
                sender_cardname="卡片名",
            ),
            headers=_headers(client),
        )
        assert response.status_code == 202, response.text
        message = store.published[0]["message"]
        assert message.sender_id == "app-user-1"
        assert message.sender_name == "外部应用"
        assert message.sender_cardname == "卡片名"

    def test_ayla_injection_uses_ayla_adapter_signature(
        self, publishing: tuple[TestClient, _PublishingStore]
    ) -> None:
        client, store = publishing
        response = client.post(
            "/chat/messages:inject",
            json=_inject_payload(
                stream_id="stream-ayla",
                platform="ayla",
                chat_type="private",
                sender_id="app-user-1",
                sender_name="汐汐",
            ),
            headers=_headers(client),
        )
        assert response.status_code == 202, response.text
        record = store.published[0]
        assert record["message"].platform == "ayla"
        assert record["message"].stream_id == "stream-ayla"
        assert record["adapter_signature"] == (
            "ayla_adapter:adapter:ayla_adapter"
        )

    def test_explicit_platform_overrides_ledger_projection(
        self, publishing: tuple[TestClient, _PublishingStore]
    ) -> None:
        # 显式指定 platform/chat_type 时：快速路径，不扫描账本，覆盖历史投影。
        # 即使用不存在的 stream_id 也能注入（平台身份由请求方声明）。
        client, store = publishing
        response = client.post(
            "/chat/messages:inject",
            json=_inject_payload(
                stream_id="stream-fresh-app",
                platform="feishu",
                chat_type="private",
            ),
            headers=_headers(client),
        )
        assert response.status_code == 202, response.text
        message = store.published[0]["message"]
        assert message.platform == "feishu"
        assert message.chat_type == "private"
        assert message.stream_id == "stream-fresh-app"

    def test_unknown_stream_returns_404(
        self, publishing: tuple[TestClient, _PublishingStore]
    ) -> None:
        client, store = publishing
        response = client.post(
            "/chat/messages:inject",
            json=_inject_payload(stream_id="stream-unknown"),
            headers=_headers(client),
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "stream_not_found"
        assert store.published == []  # 不应发布任何事件

    def test_validation_rejects_missing_content(
        self, publishing: tuple[TestClient, _PublishingStore]
    ) -> None:
        client, store = publishing
        response = client.post(
            "/chat/messages:inject",
            json={"stream_id": "stream-inject"},
            headers=_headers(client),
        )
        assert response.status_code == 422, response.text
        assert store.published == []

    def test_validation_rejects_unknown_fields(
        self, publishing: tuple[TestClient, _PublishingStore]
    ) -> None:
        client, store = publishing
        response = client.post(
            "/chat/messages:inject",
            json=_inject_payload(extra_field="nope"),
            headers=_headers(client),
        )
        assert response.status_code == 422, response.text
        assert store.published == []

    def test_requires_chat_write_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 只有 chat:read 凭据不能注入
        client, store = _build_app(tmp_path)

        class _FakeMessageReceiver:
            async def receive_message(
                self,
                message: Any,
                adapter_signature: str,
                *,
                envelope: Any = None,
            ) -> bool:
                return True

        monkeypatch.setattr(
            "src.app.api.v1.inbound_messages.get_message_receiver",
            lambda: _FakeMessageReceiver(),
        )
        store.published = []

        auth = client.app.state.api_context.store
        auth.add_credential(
            actor_id="read-only",
            audience=PLATFORM_SERVICE_AUDIENCE,
            role="platform_service",
            secret="read-only-secret-long-enough",
            scopes=("chat:read",),
            resource_grants=(),
        )
        session = client.post(
            "/auth/sessions",
            json={
                "grant_type": "service_credential",
                "audience": PLATFORM_SERVICE_AUDIENCE,
                "service_credential": "read-only-secret-long-enough",
            },
        )
        assert session.status_code == 200, session.text
        response = client.post(
            "/chat/messages:inject",
            json=_inject_payload(),
            headers={"Authorization": f"Bearer {session.json()['access_token']}"},
        )
        assert response.status_code == 403, response.text
        assert store.published == []


class TestInboundInjectorService:
    async def _inject(
        self, queries: ChatQueryService, request: InboundMessageInjectRequest
    ) -> Any:
        class _AcceptingReceiver:
            async def receive_message(
                self,
                message: Any,
                adapter_signature: str,
                *,
                envelope: Any = None,
            ) -> bool:
                return True

        return await InboundInjector(
            queries=queries,
            receiver_provider=lambda: _AcceptingReceiver(),
        ).inject(request=request, session=_session())

    def test_store_unavailable_raises_component_unavailable(
        self, tmp_path: Path
    ) -> None:
        codec = SignedValueCodec("s" * 48)
        queries = ChatQueryService(codec=codec, store_provider=lambda: None)
        request = InboundMessageInjectRequest(stream_id="stream-x", content="hello")
        with pytest.raises(APIError) as excinfo:
            asyncio.run(self._inject(queries, request))
        assert excinfo.value.status_code == 503
        assert excinfo.value.code == "component_unavailable"

    def test_unknown_stream_raises_stream_not_found(self, tmp_path: Path) -> None:
        store = _PublishingStore(tmp_path / "events.jsonl")
        asyncio.run(store.append(_chat_event("stream-inject", 1)))
        codec = SignedValueCodec("s" * 48)
        queries = ChatQueryService(codec=codec, store_provider=lambda: store)
        request = InboundMessageInjectRequest(
            stream_id="stream-unknown", content="hello"
        )
        with pytest.raises(APIError) as excinfo:
            asyncio.run(self._inject(queries, request))
        assert excinfo.value.status_code == 404
        assert excinfo.value.code == "stream_not_found"

    def test_explicit_platform_bypasses_ledger_scan(self, tmp_path: Path) -> None:
        # 显式 platform/chat_type 时走快速路径：即使账本 store 不可用也能注入。
        codec = SignedValueCodec("s" * 48)
        queries = ChatQueryService(codec=codec, store_provider=lambda: None)
        request = InboundMessageInjectRequest(
            stream_id="stream-fresh-app",
            content="hello",
            platform="feishu",
            chat_type="private",
        )
        result = asyncio.run(self._inject(queries, request))
        assert result.accepted is True
        assert result.stream_id == "stream-fresh-app"
