from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from plugins.feishu_adapter.adapter import FeishuAdapter, set_feishu_adapter
from plugins.feishu_adapter.config import FeishuAdapterConfig
from plugins.feishu_adapter.router import FeishuRouter
from src.core.models.message import MessageType
from src.core.transport.message_receive.converter import MessageConverter


class DummySink:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class DummyPlugin:
    def __init__(self, config: FeishuAdapterConfig) -> None:
        self.config = config


def make_adapter(config: FeishuAdapterConfig | None = None) -> FeishuAdapter:
    return FeishuAdapter(DummySink(), plugin=DummyPlugin(config or FeishuAdapterConfig()))


def test_feishu_config_defaults_to_long_connection() -> None:
    config = FeishuAdapterConfig()

    assert config.connection.subscription_mode == "long_connection"
    assert config.connection.auto_start_long_connection is True


@pytest.mark.asyncio
async def test_feishu_text_event_to_envelope() -> None:
    adapter = make_adapter()
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
            "token": "",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_1"},
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "text",
                "content": "{\"text\":\"爱莉爱莉\"}",
                "create_time": "1710000000000",
            },
        },
    }

    envelope = await adapter.from_platform_message(payload)

    assert envelope is not None
    assert envelope["message_info"]["platform"] == "feishu"
    assert envelope["message_info"]["message_id"] == "om_1"
    assert envelope["message_info"]["group_info"]["group_id"] == "oc_1"
    assert envelope["message_segment"][-1] == {"type": "text", "data": "爱莉爱莉"}


@pytest.mark.asyncio
async def test_feishu_handle_event_sends_to_core_sink() -> None:
    adapter = make_adapter()
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_2"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_2"}},
            "message": {
                "message_id": "om_2",
                "chat_id": "oc_2",
                "chat_type": "p2p",
                "message_type": "text",
                "content": "{\"text\":\"你好\"}",
            },
        },
    }

    result = await adapter.handle_event(payload)

    assert result["success"] is True
    assert adapter.core_sink.messages[0]["message_info"]["user_info"]["user_id"] == "ou_2"


@pytest.mark.asyncio
async def test_feishu_envelope_converts_to_core_message() -> None:
    adapter = make_adapter()
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_3"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_private"},
            },
            "message": {
                "message_id": "om_private",
                "chat_id": "oc_private",
                "chat_type": "p2p",
                "message_type": "text",
                "content": "{\"text\":\"爱莉爱莉\"}",
                "create_time": "1710000000000",
            },
        },
    }

    envelope = await adapter.from_platform_message(payload)
    assert envelope is not None

    message = await MessageConverter().envelope_to_message(envelope)

    assert message.platform == "feishu"
    assert message.message_id == "om_private"
    assert message.message_type == MessageType.TEXT
    assert message.content == "爱莉爱莉"
    assert message.extra["feishu_message_type"] == "text"
    assert "message_type" not in message.extra


@pytest.mark.asyncio
async def test_feishu_deduplicates_by_message_id() -> None:
    adapter = make_adapter()

    def payload(event_id: str):
        return {
            "schema": "2.0",
            "header": {"event_id": event_id},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_2"}},
                "message": {
                    "message_id": "om_same",
                    "chat_id": "oc_2",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": "{\"text\":\"你好\"}",
                },
            },
        }

    first = await adapter.handle_event(payload("evt_a"))
    second = await adapter.handle_event(payload("evt_b"))

    assert first["success"] is True
    assert second["ignored"] is True
    assert len(adapter.core_sink.messages) == 1


@pytest.mark.asyncio
async def test_feishu_outgoing_group_text(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter()
    calls = []

    async def fake_post(path, body):
        calls.append((path, body))
        return {"code": 0}

    monkeypatch.setattr(adapter, "_post_json", fake_post)

    await adapter._send_platform_message({
        "direction": "outgoing",
        "message_info": {
            "platform": "feishu",
            "message_id": "out_1",
            "time": 1.0,
            "group_info": {"platform": "feishu", "group_id": "oc_1", "group_name": ""},
            "user_info": {"platform": "feishu", "user_id": "ou_1", "user_nickname": ""},
        },
        "message_segment": [{"type": "text", "data": "收到"}],
    })

    assert calls[0][0] == "/open-apis/im/v1/messages?receive_id_type=chat_id"
    assert calls[0][1]["receive_id"] == "oc_1"


@pytest.mark.asyncio
async def test_feishu_outgoing_reply_text(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter()
    calls = []

    async def fake_post(path, body):
        calls.append((path, body))
        return {"code": 0}

    monkeypatch.setattr(adapter, "_post_json", fake_post)

    await adapter._send_platform_message({
        "direction": "outgoing",
        "message_info": {
            "platform": "feishu",
            "message_id": "out_2",
            "time": 1.0,
            "user_info": {"platform": "feishu", "user_id": "ou_1", "user_nickname": ""},
        },
        "message_segment": [
            {"type": "reply", "data": "om_1"},
            {"type": "text", "data": "收到"},
        ],
    })

    assert calls[0][0] == "/open-apis/im/v1/messages/om_1/reply"


def test_lark_event_object_to_payload() -> None:
    event = SimpleNamespace(
        header=SimpleNamespace(
            event_id="evt_1",
            event_type="im.message.receive_v1",
            token="token_1",
        ),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_1", user_id="user_1", union_id="union_1"),
            ),
            message=SimpleNamespace(
                message_id="om_1",
                root_id="",
                parent_id="",
                create_time=1710000000000,
                chat_id="oc_1",
                chat_type="p2p",
                message_type="text",
                content="{\"text\":\"你好\"}",
                mentions=[],
            ),
        ),
    )

    payload = FeishuAdapter._lark_event_to_payload(event)

    assert payload["header"]["event_id"] == "evt_1"
    assert payload["event"]["sender"]["sender_id"]["open_id"] == "ou_1"
    assert payload["event"]["message"]["message_id"] == "om_1"
    assert payload["event"]["message"]["content"] == "{\"text\":\"你好\"}"


def test_feishu_router_url_verification() -> None:
    config = FeishuAdapterConfig()
    config.app.verification_token = "token_1"
    adapter = make_adapter(config)
    set_feishu_adapter(adapter)
    router = FeishuRouter(plugin=DummyPlugin(config))

    client = TestClient(router.app)
    response = client.post(
        "/events",
        json={"type": "url_verification", "token": "token_1", "challenge": "ok"},
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "ok"}
    set_feishu_adapter(None)


def test_feishu_router_status_reports_connection_mode() -> None:
    config = FeishuAdapterConfig()
    adapter = make_adapter(config)
    set_feishu_adapter(adapter)
    router = FeishuRouter(plugin=DummyPlugin(config))

    client = TestClient(router.app)
    response = client.get("/api/status")

    assert response.status_code == 200
    data = response.json()
    assert data["subscription_mode"] == "long_connection"
    assert data["connected"] is False
    set_feishu_adapter(None)
