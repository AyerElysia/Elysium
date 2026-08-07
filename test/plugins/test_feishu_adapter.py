from __future__ import annotations

import asyncio
import json
import logging
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from plugins.feishu_adapter import adapter as feishu_adapter_module
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
    assert config.connection.long_connection_log_level == "WARNING"


def test_lark_sdk_log_filter_redacts_connection_credentials() -> None:
    sdk_filter = feishu_adapter_module._LarkSdkLogFilter()
    record = logging.LogRecord(
        name="Lark",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "connect failed, url=wss://example.test/ws?device_id=public"
            "&access_key=secret-access&ticket=secret-ticket&service_id=1"
        ),
        args=(),
        exc_info=None,
    )

    assert sdk_filter.filter(record) is True
    rendered = record.getMessage()
    assert "secret-access" not in rendered
    assert "secret-ticket" not in rendered
    assert "access_key=<redacted>" in rendered
    assert "ticket=<redacted>" in rendered


def test_lark_sdk_log_filter_suppresses_routine_reconnect_chatter() -> None:
    sdk_filter = feishu_adapter_module._LarkSdkLogFilter()

    for message in (
        "connected to wss://example.test/ws?access_key=secret&ticket=secret",
        "disconnected to wss://example.test/ws?access_key=secret&ticket=secret",
        "trying to reconnect for the 2nd time",
        "receive message loop exit, err: connection closed",
    ):
        record = logging.LogRecord(
            name="Lark",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )

        assert sdk_filter.filter(record) is False
        assert "secret" not in record.getMessage()


def test_lark_sdk_log_filter_rate_limits_repeated_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(feishu_adapter_module.time, "monotonic", lambda: now)
    sdk_filter = feishu_adapter_module._LarkSdkLogFilter()

    def make_record() -> logging.LogRecord:
        return logging.LogRecord(
            name="Lark",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="connect failed, err: temporary network failure",
            args=(),
            exc_info=None,
        )

    assert sdk_filter.filter(make_record()) is True
    assert sdk_filter.filter(make_record()) is False
    now += feishu_adapter_module._LARK_REPEAT_LOG_INTERVAL_SECONDS
    assert sdk_filter.filter(make_record()) is True


async def test_feishu_long_connection_uses_dedicated_sdk_event_loop(monkeypatch) -> None:
    """The SDK must not run its blocking loop on the bot's main event loop."""
    import lark_oapi.ws.client as ws_client_module

    config = FeishuAdapterConfig()
    config.app.app_id = "cli_test"
    config.app.app_secret = "secret_test"
    adapter = make_adapter(config)
    observed_loops: list[asyncio.AbstractEventLoop] = []

    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        def start(self) -> None:
            sdk_loop = ws_client_module.loop
            observed_loops.append(sdk_loop)
            sdk_loop.run_until_complete(asyncio.sleep(0))
            adapter._feishu_stop_event.set()

    main_loop = asyncio.get_running_loop()
    previous_sdk_loop = ws_client_module.loop
    monkeypatch.setattr(
        feishu_adapter_module,
        "_lark_oapi_ws_module",
        SimpleNamespace(Client=FakeClient),
    )
    monkeypatch.setattr(adapter, "_build_lark_event_handler", lambda _: object())
    ws_client_module.loop = main_loop
    try:
        await asyncio.to_thread(adapter._run_long_connection_client)
    finally:
        ws_client_module.loop = previous_sdk_loop

    assert observed_loops
    assert observed_loops[0] is not main_loop
    assert observed_loops[0].is_closed()


async def test_feishu_http_client_is_reused_and_closed() -> None:
    """飞书 API 请求应复用连接池，并在适配器卸载时关闭。"""
    adapter = make_adapter()

    first = await adapter._get_http_client()
    second = await adapter._get_http_client()

    assert first is second
    await adapter.on_adapter_unloaded()
    assert first.is_closed
    assert adapter._http_client is None


async def test_feishu_token_refresh_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发发送时只允许一次 tenant token 刷新请求。"""
    adapter = make_adapter(_credentialed_config())
    request = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "tenant_access_token": "token",
                "expire": 7200,
            },
        )
    )
    monkeypatch.setattr(adapter, "_request_with_retry", request)

    tokens = await asyncio.gather(
        *(adapter._get_tenant_access_token() for _ in range(10))
    )

    assert tokens == ["token"] * 10
    request.assert_awaited_once()


def test_feishu_watchdog_refuses_duplicate_client_for_stuck_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 SDK 线程未退出时不能再启动第二个共享事件循环客户端。"""
    adapter = make_adapter()
    adapter._long_connection_thread = MagicMock(is_alive=lambda: True)
    start = MagicMock()
    monkeypatch.setattr(adapter, "_start_long_connection", start)

    adapter._force_restart_long_connection()

    start.assert_not_called()


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


async def test_feishu_image_event_downloads_to_image_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter()
    downloaded: dict[str, str] = {}

    async def fake_download_resource(*, message_id: str, resource_key: str, resource_type: str) -> str:
        downloaded.update(
            {
                "message_id": message_id,
                "resource_key": resource_key,
                "resource_type": resource_type,
            }
        )
        return "ZmFrZV9pbWFnZQ=="

    monkeypatch.setattr(adapter, "_download_message_resource_as_base64", fake_download_resource)

    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_image"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_image",
                "chat_id": "oc_private",
                "chat_type": "p2p",
                "message_type": "image",
                "content": "{\"image_key\":\"img_v3_demo\"}",
            },
        },
    }

    envelope = await adapter.from_platform_message(payload)

    assert envelope is not None
    assert envelope["message_segment"] == [
        {"type": "image", "data": "ZmFrZV9pbWFnZQ=="}
    ]
    assert envelope["message_info"]["extra"]["feishu_media_refs"] == [
        {"type": "image", "key": "img_v3_demo"}
    ]
    assert downloaded == {
        "message_id": "om_image",
        "resource_key": "img_v3_demo",
        "resource_type": "image",
    }


async def test_feishu_audio_event_downloads_to_voice_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter()
    downloaded: dict[str, str] = {}

    async def fake_download_resource(*, message_id: str, resource_key: str, resource_type: str) -> str:
        downloaded.update({
            "message_id": message_id,
            "resource_key": resource_key,
            "resource_type": resource_type,
        })
        return "ZmFrZV9hdWRpbw=="

    monkeypatch.setattr(adapter, "_download_message_resource_as_base64", fake_download_resource)
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_audio"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_audio",
                "chat_id": "oc_private",
                "chat_type": "p2p",
                "message_type": "audio",
                "content": "{\"file_key\":\"file_v3_demo\"}",
            },
        },
    }

    envelope = await adapter.from_platform_message(payload)

    assert envelope is not None
    assert envelope["message_segment"] == [
        {
            "type": "voice",
            "data": {
                "base64": "ZmFrZV9hdWRpbw==",
                "mime_type": "audio/ogg",
                "filename": "file_v3_demo.opus",
            },
        }
    ]
    assert downloaded == {
        "message_id": "om_audio",
        "resource_key": "file_v3_demo",
        "resource_type": "file",
    }


async def test_feishu_image_event_falls_back_to_text_when_download_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter()

    async def fake_download_resource(*_args, **_kwargs) -> str:
        raise RuntimeError("missing permission")

    monkeypatch.setattr(adapter, "_download_message_resource_as_base64", fake_download_resource)

    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_image_fallback"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_image_fallback",
                "chat_id": "oc_private",
                "chat_type": "p2p",
                "message_type": "image",
                "content": "{\"image_key\":\"img_v3_demo\"}",
            },
        },
    }

    envelope = await adapter.from_platform_message(payload)

    assert envelope is not None
    assert envelope["message_segment"] == [{"type": "text", "data": "[图片]"}]


async def test_feishu_persisted_identity_replaces_config_alias_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter()

    async def persisted_identity(account_id: str) -> tuple[str, str]:
        assert account_id == "ou_peach"
        return "桃子哥", "wander_hunter"

    monkeypatch.setattr(adapter, "_persisted_identity", persisted_identity)
    monkeypatch.setattr(
        adapter,
        "_resolve_display_name",
        AsyncMock(side_effect=AssertionError("directory lookup must not run")),
    )

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="", message_id="om_database_identity")
    )

    assert envelope is not None
    assert _nickname(envelope) == "桃子哥"
    extra = envelope["message_info"]["extra"]
    assert extra["canonical_person_key"] == "wander_hunter"
    assert extra["identity_resolution_status"] == "resolved"
    assert extra["identity_display_name_source"] == "person_info"


async def test_feishu_user_name_alias_maps_sender_display_name() -> None:
    config = FeishuAdapterConfig()
    config.identity.user_name_aliases = [
        "ou_ayer=AyerElysia",
        "on_ayer=AyerElysia",
    ]
    adapter = make_adapter(config)
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_alias"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {
                    "open_id": "ou_ayer",
                    "union_id": "on_ayer",
                },
            },
            "message": {
                "message_id": "om_alias",
                "chat_id": "oc_private",
                "chat_type": "p2p",
                "message_type": "text",
                "content": "{\"text\":\"笨蛋爱莉\"}",
                "create_time": "1710000000000",
            },
        },
    }

    envelope = await adapter.from_platform_message(payload)

    assert envelope is not None
    user_info = envelope["message_info"]["user_info"]
    extra = envelope["message_info"]["extra"]
    assert user_info["user_id"] == "ou_ayer"
    assert user_info["user_nickname"] == "AyerElysia"
    assert extra["feishu_open_id"] == "ou_ayer"
    assert extra["feishu_union_id"] == "on_ayer"


async def test_feishu_explicit_identity_mapping_reaches_envelope() -> None:
    config = FeishuAdapterConfig()
    config.identity.user_name_aliases = ["ou_peach=Wander Hunter（桃子哥）"]
    config.identity.canonical_identity_aliases = [
        "ou_peach=wander_hunter",
        "on_peach=wander_hunter",
    ]
    adapter = make_adapter(config)

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_identity")
    )

    assert envelope is not None
    extra = envelope["message_info"]["extra"]
    assert _nickname(envelope) == "Wander Hunter（桃子哥）"
    assert extra["sender_platform_account_key"] == "feishu:ou_peach"
    assert extra["canonical_person_key"] == "wander_hunter"
    assert extra["identity_resolution_status"] == "resolved"
    assert extra["identity_display_name_source"] == "configured_alias"


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


async def test_feishu_outgoing_group_text(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter()
    calls = []

    async def fake_post(path, body):
        calls.append((path, body))
        return {
            "code": 0,
            "data": {"message_id": "om_sent_1", "chat_id": "oc_1"},
        }

    monkeypatch.setattr(adapter, "_post_json", fake_post)

    receipt = await adapter._send_platform_message({
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
    assert receipt == {
        "code": 0,
        "data": {"message_id": "om_sent_1", "chat_id": "oc_1"},
    }


def test_feishu_oversized_image_is_compressed_for_upload() -> None:
    from PIL import Image as PILImage

    source = PILImage.effect_noise((5000, 5000), 100)
    raw = BytesIO()
    source.save(raw, format="JPEG", quality=100)
    original = raw.getvalue()
    upload, mime = FeishuAdapter._prepare_image_upload_bytes(original)

    assert len(original) > feishu_adapter_module._FEISHU_IMAGE_UPLOAD_MAX_BYTES
    assert mime == "image/jpeg"
    assert len(upload) <= feishu_adapter_module._FEISHU_IMAGE_UPLOAD_MAX_BYTES
    assert raw.getvalue() != upload


async def test_feishu_outgoing_image_uploads_and_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter()
    calls = []

    async def fake_upload_image(image_data: str) -> str:
        assert image_data == "ZmFrZV9pbWFnZQ=="
        return "img-key-1"

    async def fake_post(path, body):
        calls.append((path, body))
        return {"code": 0}

    monkeypatch.setattr(adapter, "_upload_image_data", fake_upload_image)
    monkeypatch.setattr(adapter, "_post_json", fake_post)

    await adapter._send_platform_message({
        "direction": "outgoing",
        "message_info": {
            "platform": "feishu",
            "message_id": "out_image",
            "time": 1.0,
            "user_info": {"platform": "feishu", "user_id": "ou_1", "user_nickname": ""},
        },
        "message_segment": [{"type": "image", "data": "ZmFrZV9pbWFnZQ=="}],
    })

    assert calls[0][0] == "/open-apis/im/v1/messages?receive_id_type=open_id"
    assert calls[0][1]["receive_id"] == "ou_1"
    assert calls[0][1]["msg_type"] == "image"
    assert json.loads(calls[0][1]["content"]) == {"image_key": "img-key-1"}


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


def test_feishu_audio_conversion_reports_missing_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_transcode(_audio_bytes: bytes) -> bytes:
        raise RuntimeError("音频转码需要 FFmpeg")

    monkeypatch.setattr(
        "plugins.feishu_adapter.adapter.transcode_audio_to_opus",
        fail_transcode,
    )

    with pytest.raises(RuntimeError, match="FFmpeg"):
        FeishuAdapter._convert_audio_to_opus(b"RIFF-demo")


async def test_feishu_outgoing_voice_sends_audio_message(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter()
    calls = []

    async def fake_upload_audio(voice_data: str):
        assert voice_data == "UklGRg=="
        return "file-key-1", 1200

    async def fake_post(path, body):
        calls.append((path, body))
        return {"code": 0}

    monkeypatch.setattr(adapter, "_upload_audio", fake_upload_audio)
    monkeypatch.setattr(adapter, "_post_json", fake_post)

    await adapter._send_platform_message({
        "direction": "outgoing",
        "message_info": {
            "platform": "feishu",
            "message_id": "out_voice",
            "time": 1.0,
            "user_info": {"platform": "feishu", "user_id": "ou_1", "user_nickname": ""},
        },
        "message_segment": [{"type": "voice", "data": "UklGRg=="}],
    })

    assert calls[0][0] == "/open-apis/im/v1/messages?receive_id_type=open_id"
    assert calls[0][1]["receive_id"] == "ou_1"
    assert calls[0][1]["msg_type"] == "audio"
    assert json.loads(calls[0][1]["content"]) == {
        "file_key": "file-key-1",
        "duration": 1200,
    }


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
    assert data["identity"]["status"] == "unknown"
    set_feishu_adapter(None)


# --- 显示名解析回归测试 -------------------------------------------------------
# 飞书消息事件的 sender 里只有 ID，没有 display name。以前兜底会把 union_id/open_id
# 直接当人名塞给上游，于是她看到的是 on_41a2efd3... 这种串，同一个群里几个人全长这样，
# 自然就认错人。下面这些用例锁住"把 ID 换成真名"这条链路。


def _credentialed_config() -> FeishuAdapterConfig:
    """带 app 凭据的配置：没凭据时解析会直接短路，测不到接口分支。"""
    config = FeishuAdapterConfig()
    config.app.app_id = "cli_test"
    config.app.app_secret = "secret_test"
    return config


def _event(
    *,
    open_id: str,
    union_id: str = "",
    message_id: str,
    text: str = "在吗",
    mentions: list | None = None,
    chat_id: str = "oc_group",
) -> dict:
    sender_id: dict[str, str] = {"open_id": open_id}
    if union_id:
        sender_id["union_id"] = union_id
    return {
        "schema": "2.0",
        "header": {"event_id": f"evt_{message_id}"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": sender_id},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "create_time": "1710000000000",
                "mentions": mentions or [],
            },
        },
    }


def _nickname(envelope: dict) -> str:
    return envelope["message_info"]["user_info"]["user_nickname"]


class FakeApi:
    """假的开放平台接口：按路径关键字返回预置响应或抛错，并记录调用次数。"""

    def __init__(self, *, contact: dict | None = None, members: dict | None = None) -> None:
        self.contact = contact
        self.members = members
        self.paths: list[str] = []

    async def __call__(self, path: str) -> dict:
        self.paths.append(path)
        if "contact/v3/users" in path:
            if self.contact is None:
                raise RuntimeError("Feishu API failed: code=99991672 no permission")
            return self.contact
        if "chats/" in path and "members" in path:
            if self.members is None:
                raise RuntimeError("Feishu API failed: code=99991672 no permission")
            return self.members
        raise AssertionError(f"unexpected path: {path}")

    @property
    def contact_calls(self) -> int:
        return sum(1 for p in self.paths if "contact/v3/users" in p)

    @property
    def member_calls(self) -> int:
        return sum(1 for p in self.paths if "members" in p)


def _contact_response(name: str) -> dict:
    return {"code": 0, "data": {"user": {"name": name}}}


def _members_response(member_id: str, name: str) -> dict:
    return {"code": 0, "data": {"items": [{"member_id": member_id, "name": name}]}}


def test_looks_like_raw_id_recognizes_feishu_prefixes() -> None:
    assert FeishuAdapter._looks_like_raw_id("on_41a2efd323503ed77bd4ce206f309db7") is True
    assert FeishuAdapter._looks_like_raw_id("ou_90386a0f566828037ff9c29ebacdc4cb") is True
    assert FeishuAdapter._looks_like_raw_id("") is True
    assert FeishuAdapter._looks_like_raw_id("   ") is True
    assert FeishuAdapter._looks_like_raw_id("桃子哥") is False
    assert FeishuAdapter._looks_like_raw_id("AyerElysia") is False


async def test_resolved_name_replaces_raw_id_via_contact_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通讯录接口能查到名字时，上游拿到的就是真名，而不是 on_xxx。"""
    adapter = make_adapter(_credentialed_config())
    api = FakeApi(contact=_contact_response("桃子哥"))
    monkeypatch.setattr(adapter, "_get_json", api)

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_1")
    )

    assert envelope is not None
    assert _nickname(envelope) == "桃子哥"
    assert api.contact_calls == 1
    # 通讯录已经给出名字，不该再去翻群成员列表
    assert api.member_calls == 0


async def test_resolve_falls_back_to_chat_members_when_contact_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通讯录权限没开（403）时，用群成员列表兜底。"""
    adapter = make_adapter(_credentialed_config())
    api = FakeApi(contact=None, members=_members_response("ou_peach", "桃子哥"))
    monkeypatch.setattr(adapter, "_get_json", api)

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_1")
    )

    assert envelope is not None
    assert _nickname(envelope) == "桃子哥"
    assert api.contact_calls == 1
    assert api.member_calls == 1


async def test_resolved_name_is_cached_across_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个人连发多条，只查一次接口。"""
    adapter = make_adapter(_credentialed_config())
    api = FakeApi(contact=_contact_response("桃子哥"))
    monkeypatch.setattr(adapter, "_get_json", api)

    for index in range(3):
        envelope = await adapter.from_platform_message(
            _event(open_id="ou_peach", union_id="on_peach", message_id=f"om_{index}")
        )
        assert envelope is not None
        assert _nickname(envelope) == "桃子哥"

    assert api.contact_calls == 1


async def test_failed_resolution_keeps_raw_id_and_negative_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个接口都失败时保留原兜底：消息照常投递，且不会每条都去撞接口。"""
    adapter = make_adapter(_credentialed_config())
    api = FakeApi(contact=None, members=None)
    monkeypatch.setattr(adapter, "_get_json", api)

    first = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_1")
    )
    second = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_2")
    )

    # 取名失败绝不能丢消息，但也不能继续把原始 ID 伪装成人名。
    assert first is not None
    assert second is not None
    assert _nickname(first) == _nickname(second)
    assert _nickname(first).startswith("身份未解析的飞书用户")
    assert "on_peach" not in _nickname(first)
    assert first["message_info"]["extra"]["identity_resolution_status"] == "unresolved"
    # 负缓存生效：第二条不再打接口
    assert api.contact_calls == 1
    assert api.member_calls == 1
    health = adapter.identity_health_snapshot()
    assert health["status"] == "degraded"
    assert health["unresolved_messages"] == 2
    assert health["negative_cache_entries"] == 2
    assert health["last_failure_reason"] == "chat_members:permission_denied"


async def test_negative_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 过期后允许重试——权限后来补上了也能自己恢复。"""
    config = _credentialed_config()
    config.identity.display_name_negative_cache_ttl = 60.0
    adapter = make_adapter(config)
    api = FakeApi(contact=None, members=None)
    monkeypatch.setattr(adapter, "_get_json", api)

    await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_1")
    )
    assert api.contact_calls == 1

    # 把缓存写入时间往前拨，模拟 TTL 过期
    for key in list(adapter._display_name_cached_at):
        adapter._display_name_cached_at[key] -= 120.0
    api.contact = _contact_response("桃子哥")

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_2")
    )

    assert envelope is not None
    assert _nickname(envelope) == "桃子哥"
    assert api.contact_calls == 2


async def test_mention_name_is_harvested_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@ 段里飞书会带 name。别人 @ 过他之后，他自己发言就有名字了，且不用打接口。"""
    adapter = make_adapter(_credentialed_config())
    api = FakeApi(contact=None, members=None)
    monkeypatch.setattr(adapter, "_get_json", api)

    # 甲 @ 了桃子哥：白捡一条 ID -> 真名的映射
    await adapter.from_platform_message(
        _event(
            open_id="ou_other",
            union_id="on_other",
            message_id="om_mention",
            text="@桃子哥 看这个",
            mentions=[{"key": "@_user_1", "name": "桃子哥", "id": {"open_id": "ou_peach"}}],
        )
    )
    calls_after_mention = api.contact_calls

    # 桃子哥自己发言，直接命中缓存
    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_self")
    )

    assert envelope is not None
    assert _nickname(envelope) == "桃子哥"
    assert api.contact_calls == calls_after_mention


async def test_alias_still_wins_over_api_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手配的 alias 优先级最高，配了就不打接口。"""
    config = _credentialed_config()
    config.identity.user_name_aliases = ["ou_peach=桃子哥"]
    adapter = make_adapter(config)
    api = FakeApi(contact=_contact_response("接口返回的名字"))
    monkeypatch.setattr(adapter, "_get_json", api)

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_1")
    )

    assert envelope is not None
    assert _nickname(envelope) == "桃子哥"
    assert api.contact_calls == 0


async def test_resolution_disabled_skips_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关掉后使用未解析标签，一个接口都不打。"""
    config = _credentialed_config()
    config.identity.resolve_display_names = False
    adapter = make_adapter(config)
    api = FakeApi(contact=_contact_response("桃子哥"))
    monkeypatch.setattr(adapter, "_get_json", api)

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_1")
    )

    assert envelope is not None
    assert _nickname(envelope).startswith("身份未解析的飞书用户")
    assert envelope["message_info"]["extra"]["identity_resolution_status"] == "unresolved"
    assert api.paths == []


async def test_missing_credentials_skip_api_without_negative_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配 app 凭据时不去打接口（拿不到 token），但也不写负缓存。

    凭据补上以后应该立刻能解析，不该被负缓存压到 TTL 过期。
    """
    adapter = make_adapter()  # 默认配置没有 app_id/app_secret
    api = FakeApi(contact=_contact_response("桃子哥"))
    monkeypatch.setattr(adapter, "_get_json", api)

    envelope = await adapter.from_platform_message(
        _event(open_id="ou_peach", union_id="on_peach", message_id="om_1")
    )
    assert envelope is not None
    assert _nickname(envelope).startswith("身份未解析的飞书用户")
    assert api.paths == []
    assert adapter._display_name_cache == {}


async def test_mention_harvest_ignores_id_shaped_names() -> None:
    """mentions 里如果 name 本身就是个 ID，不能当成真名缓存起来。"""
    adapter = make_adapter()

    adapter._harvest_mention_names(
        [
            {"name": "ou_90386a0f566828037ff9c29ebacdc4cb", "id": {"open_id": "ou_a"}},
            {"name": "", "id": {"open_id": "ou_b"}},
            {"name": "桃子哥", "id": {"open_id": "ou_c"}},
            "not-a-dict",
        ]
    )

    assert adapter._display_name_cache == {"ou_c": "桃子哥"}
