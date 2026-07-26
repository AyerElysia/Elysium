from __future__ import annotations

import base64
import io
import json
import urllib.parse
import wave

import pytest

from plugins.minicpm_live_bridge.realtime_adapter import (
    MiniCPMRealtimeAdapter,
    PassthroughRealtimeAdapter,
    build_realtime_adapter,
)


def _make_wav_base64(samples: list[float], sample_rate: int = 16000) -> str:
    pcm16 = bytearray()
    for sample in samples:
        value = max(-1.0, min(1.0, sample))
        pcm16.extend(int(value * 32767).to_bytes(2, byteorder="little", signed=True))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(pcm16))
    return "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


async def test_build_realtime_adapter_returns_minicpm_adapter() -> None:
    adapter = build_realtime_adapter(
        adapter_name="minicpm_realtime_v0",
        session_id="live-session",
        upstream_url="ws://127.0.0.1:8010/v1/realtime?mode=video",
        upstream_headers={"Authorization": "Bearer test"},
    )

    assert isinstance(adapter, MiniCPMRealtimeAdapter)
    parsed = urllib.parse.urlparse(adapter.upstream_connect_url())
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "ws"
    assert parsed.netloc == "127.0.0.1:8010"
    assert parsed.path == "/v1/realtime"
    assert query["mode"] == ["video"]
    assert query["uid"] == ["live-session"]
    assert adapter.upstream_connect_headers() == {"Authorization": "Bearer test"}


async def test_minicpm_adapter_builds_session_update_from_context_snapshot() -> None:
    adapter = MiniCPMRealtimeAdapter(
        session_id="live-session",
        upstream_url="ws://127.0.0.1:8010/v1/realtime?mode=video",
        upstream_headers={},
    )

    await adapter.on_client_message({"type": "session.start", "session_id": "live-session"})
    result = await adapter.on_client_message(
        {
            "type": "context.snapshot",
            "session_id": "live-session",
            "context": {
                "life_runtime_context": "ctx",
                "life_chatter_prompt": {
                    "system_prompt": "system",
                    "user_prompt": "user prompt",
                    "dynamic_context": "dyn",
                },
                "unified_events": [{"text": "QQ 来了一条消息", "event_type": "on_message_received"}],
                "session_events": [{"text": "session event"}],
            },
        }
    )

    assert result.upstream_messages == []
    assert result.client_messages == [
        {"type": "status", "status": "context.snapshot", "text": "context snapshot received"}
    ]
    assert adapter.context_snapshot["life_runtime_context"] == "ctx"


async def test_minicpm_adapter_turns_pcm_audio_into_append_and_attaches_video_frame() -> None:
    adapter = MiniCPMRealtimeAdapter(
        session_id="live-session",
        upstream_url="ws://127.0.0.1:8010/v1/realtime?mode=video",
        upstream_headers={},
    )

    await adapter.on_client_message(
        {
            "type": "context.snapshot",
            "session_id": "live-session",
            "context": {"life_runtime_context": "ctx"},
        }
    )
    await adapter.on_client_message(
        {
            "type": "screen.frame",
            "session_id": "live-session",
            "timestamp": 123,
            "width": 1280,
            "height": 720,
            "image": "data:image/jpeg;base64,AAAA",
        }
    )
    result = await adapter.on_client_message(
        {
            "type": "audio.chunk",
            "session_id": "live-session",
            "timestamp": 456,
            "encoding": "pcm_float32",
            "sample_rate": 16000,
            "channels": 1,
            "data": "data:audio/pcm;encoding=float32;rate=16000;base64," + base64.b64encode(b"\x00" * 64).decode("ascii"),
        }
    )

    assert len(result.upstream_messages) == 1
    audio_packet = json.loads(result.upstream_messages[0])
    content = audio_packet["messages"][0]["content"]
    assert content[0]["type"] == "input_audio"
    assert content[0]["input_audio"]["data"]
    assert content[1] == {"type": "image_data", "image_data": {"data": "AAAA"}}


async def test_minicpm_adapter_converts_wav_turn_and_emits_client_audio_and_final() -> None:
    adapter = MiniCPMRealtimeAdapter(
        session_id="live-session",
        upstream_url="ws://127.0.0.1:8010/v1/realtime?mode=audio",
        upstream_headers={},
    )

    result = await adapter.on_client_message(
        {
            "type": "audio.turn",
            "session_id": "live-session",
            "timestamp": 789,
            "mime_type": "audio/wav",
            "data": _make_wav_base64([0.0, 0.5, -0.5, 0.0]),
        }
    )

    assert len(result.upstream_messages) == 1
    assert json.loads(result.upstream_messages[0])["messages"][0]["content"][0]["type"] == "input_audio"

    upstream = await adapter.on_upstream_message(
        {
            "type": "response.output_audio.delta",
            "text": "你好",
            "audio": base64.b64encode(b"\x00" * 32).decode("ascii"),
            "end_of_turn": False,
            "kv_cache_length": 42,
        }
    )
    assert upstream.client_messages[0]["type"] == "partial"
    assert upstream.client_messages[1]["type"] == "audio"

    upstream_final = await adapter.on_upstream_message(
        {
            "type": "response.output_audio.delta",
            "text": "，世界",
            "audio": base64.b64encode(b"\x00" * 32).decode("ascii"),
            "end_of_turn": True,
            "kv_cache_length": 43,
        }
    )
    assert any(msg["type"] == "final" for msg in upstream_final.client_messages)


async def test_passthrough_adapter_returns_raw_upstream_payload_to_client() -> None:
    adapter = PassthroughRealtimeAdapter(
        session_id="live-session",
        upstream_url="ws://127.0.0.1:9999/live",
        upstream_headers={},
    )

    result = await adapter.on_upstream_message({"type": "final", "role": "assistant", "text": "你好"})

    assert result.upstream_messages == []
    assert result.client_messages == [{"type": "final", "role": "assistant", "text": "你好"}]
