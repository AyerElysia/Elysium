from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.minicpm_live_bridge.config import MiniCPMLiveBridgeConfig
from plugins.minicpm_live_bridge.router import LiveTurnRequest, MiniCPMLiveRouter


def _make_router(monkeypatch: pytest.MonkeyPatch) -> MiniCPMLiveRouter:
    router = object.__new__(MiniCPMLiveRouter)
    monkeypatch.setattr(router, "_log_live", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(router, "_preview", lambda value, **_kwargs: str(value))
    return router


def _attach_config(router: MiniCPMLiveRouter, config: MiniCPMLiveBridgeConfig) -> None:
    router.plugin = SimpleNamespace(config=config)


@pytest.mark.asyncio
async def test_resolve_live_turn_user_text_uses_asr_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router(monkeypatch)
    transcribe_mock = AsyncMock(return_value="你好，我来了")
    monkeypatch.setattr(router, "_transcribe_live_audio", transcribe_mock)

    request = LiveTurnRequest(
        session_id="live-session",
        audio_data="data:audio/wav;base64,AAAA",
        audio_mime_type="audio/wav",
    )

    text, payload = await router._resolve_live_turn_user_text(request)

    assert text == "你好，我来了"
    assert payload == {
        "voice_transcript": "你好，我来了",
        "voice_transcript_available": True,
        "voice_transcript_source": "neo_media_manager_asr",
    }
    transcribe_mock.assert_awaited_once_with(
        audio_data="data:audio/wav;base64,AAAA",
        audio_mime_type="audio/wav",
    )


@pytest.mark.asyncio
async def test_resolve_live_turn_user_text_falls_back_to_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router(monkeypatch)
    monkeypatch.setattr(router, "_transcribe_live_audio", AsyncMock(return_value=""))

    request = LiveTurnRequest(
        session_id="live-session",
        audio_data="data:audio/wav;base64,BBBB",
        audio_mime_type="audio/wav",
    )

    text, payload = await router._resolve_live_turn_user_text(request)

    assert text == "[语音输入]"
    assert payload == {
        "voice_transcript": "",
        "voice_transcript_available": False,
        "voice_transcript_source": "neo_media_manager_asr",
    }


@pytest.mark.asyncio
async def test_ingest_current_turn_input_preserves_voice_transcript_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _make_router(monkeypatch)
    captured: dict[str, object] = {}

    async def fake_ingest(request, local_event) -> None:
        captured["request"] = request
        captured["local_event"] = local_event

    monkeypatch.setattr(router, "_ingest_live_event", fake_ingest)

    request = LiveTurnRequest(
        session_id="live-session",
        audio_data="data:audio/wav;base64,CCCC",
        audio_mime_type="audio/wav",
        payload={"input_mode": "vad_voice"},
    )

    await router._ingest_current_turn_input(
        request=request,
        event_type="voice_input",
        text="测试转写",
        payload={
            "input_mode": "vad_voice",
            "voice_transcript": "测试转写",
            "voice_transcript_available": True,
            "voice_transcript_source": "neo_media_manager_asr",
        },
    )

    live_request = captured["request"]
    local_event = captured["local_event"]

    assert live_request.text == "测试转写"
    assert live_request.payload["voice_transcript"] == "测试转写"
    assert live_request.payload["voice_transcript_available"] is True
    assert live_request.payload["voice_transcript_source"] == "neo_media_manager_asr"
    assert live_request.payload["audio_mime_type"] == "audio/wav"
    assert live_request.payload["has_audio_data"] is True
    assert local_event["text"] == "测试转写"


def test_client_config_uses_proxy_websocket_template(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _make_router(monkeypatch)
    config = MiniCPMLiveBridgeConfig()
    config.server.base_url = "http://127.0.0.1:8010"
    config.server.websocket_url = "ws://127.0.0.1:8010/ws/api/v1/stream?uid={session_id}"
    config.server.transport_mode = "neo_proxy"
    config.server.protocol_adapter = "minicpm_realtime_v0"
    _attach_config(router, config)

    client_config = router._client_config(session_id="live-session")

    assert client_config["mode"] == "neo_proxy_ws"
    assert client_config["server"]["transport_mode"] == "neo_proxy"
    assert client_config["server"]["protocol_adapter"] == "minicpm_realtime_v0"
    assert client_config["server"]["websocket_url"] == (
        "/minicpm-live/api/realtime/ws?session_id=live-session&api_key={api_key}"
    )
    assert client_config["server"]["upstream_websocket_url"] == (
        "ws://127.0.0.1:8010/ws/api/v1/stream?uid=live-session"
    )


def test_client_config_keeps_direct_websocket_in_browser_direct_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _make_router(monkeypatch)
    config = MiniCPMLiveBridgeConfig()
    config.server.base_url = "http://127.0.0.1:8010"
    config.server.websocket_url = "/ws/api/v1/stream?uid={session_id}"
    config.server.transport_mode = "browser_direct"
    _attach_config(router, config)

    client_config = router._client_config(session_id="live-session")

    assert client_config["mode"] == "ws_ingest"
    assert client_config["server"]["websocket_url"] == (
        "ws://127.0.0.1:8010/ws/api/v1/stream?uid=live-session"
    )
    assert client_config["server"]["upstream_websocket_url"] == (
        "ws://127.0.0.1:8010/ws/api/v1/stream?uid=live-session"
    )
