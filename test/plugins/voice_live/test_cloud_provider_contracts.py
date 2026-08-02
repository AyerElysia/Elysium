from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from aiohttp import web

from plugins.voice_live.providers.base import AudioDelta, TranscriptEvent
from plugins.voice_live.providers.openai_realtime import OpenAIRealtimeProvider
from plugins.voice_live.providers.qwen_realtime import (
    QwenRealtimeProvider,
    _qwen_safe_tool_name,
)


async def _start_server(handler: Any) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_get("/realtime", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_qwen_realtime_wire_contract() -> None:
    observed: dict[str, Any] = {}
    done = asyncio.Event()

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        observed["authorization"] = request.headers.get("Authorization")
        observed["model"] = request.query.get("model")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session.created", "session": {}})
        observed["sessions"] = []
        for _ in range(3):
            observed["sessions"].append(json.loads((await ws.receive()).data))
            await ws.send_json({"type": "session.updated", "session": {}})
        observed["audio"] = json.loads((await ws.receive()).data)
        pcm = b"\x01\x00" * 240
        await ws.send_json(
            {"type": "response.created", "response": {"id": "qwen-r1"}}
        )
        await ws.send_json(
            {"type": "response.audio.delta", "delta": base64.b64encode(pcm).decode()}
        )
        await ws.send_json(
            {
                "type": "response.audio_transcript.done",
                "event_id": "qwen-e1",
                "transcript": "已接通",
            }
        )
        await ws.send_json({"type": "response.done", "response": {"usage": {"total_tokens": 2}}})
        done.set()
        async for _ in ws:
            pass
        return ws

    runner, port = await _start_server(websocket)
    provider = QwenRealtimeProvider(
        f"ws://127.0.0.1:{port}/realtime",
        "secret-value",
        model="qwen3.5-omni-plus-realtime",
        connect_timeout=2,
        event_timeout=2,
    )
    audio: list[AudioDelta] = []
    transcripts: list[TranscriptEvent] = []

    async def on_audio(event: AudioDelta) -> None:
        audio.append(event)

    async def on_transcript(event: TranscriptEvent) -> None:
        transcripts.append(event)

    provider.on_audio_delta(on_audio)
    provider.on_transcript(on_transcript)
    try:
        await provider.connect(
            {
                "instructions": "identity",
                "tools": [
                    {
                        "type": "function",
                        "name": "remember",
                        "description": "store a memory",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )
        await provider.send_audio(b"\x00\x00" * 320)
        await asyncio.wait_for(done.wait(), timeout=2)
        await asyncio.sleep(0.05)
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert observed["authorization"] == "Bearer secret-value"
    assert observed["model"] == "qwen3.5-omni-plus-realtime"
    session = observed["sessions"][0]["session"]
    assert session["turn_detection"] == {"type": "semantic_vad"}
    assert session["input_audio_format"] == "pcm"
    assert observed["sessions"][1]["session"] == {"instructions": "identity"}
    assert observed["sessions"][2]["session"]["tools"][0]["function"]["name"] == "remember"
    assert len(base64.b64decode(observed["audio"]["audio"])) == 640
    assert audio[0].sample_rate == 24000
    assert transcripts[-1] == TranscriptEvent("assistant", "已接通", True, "qwen-e1")


def test_qwen_tool_name_mapping_is_reversible_and_protocol_safe() -> None:
    assert _qwen_safe_tool_name("action-report_state") == "action_report_state"


@pytest.mark.asyncio
async def test_qwen_interrupt_is_idempotent_without_an_active_response() -> None:
    class FakeWebSocket:
        closed = False

        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        async def send_str(self, payload: str) -> None:
            self.events.append(json.loads(payload))

    provider = QwenRealtimeProvider(
        "ws://127.0.0.1/realtime",
        "secret-value",
        model="qwen-audio-3.0-realtime-plus",
    )
    websocket = FakeWebSocket()
    provider._ws = websocket  # type: ignore[assignment]

    await provider.interrupt(played_audio_ms=125)
    assert websocket.events == []

    provider._response_active = True
    provider._active_response_id = "response-1"
    provider._active_item_id = "item-1"
    await provider.interrupt(played_audio_ms=125)

    assert [event["type"] for event in websocket.events] == [
        "response.cancel",
        "conversation.item.truncate",
    ]
    assert websocket.events[1]["audio_end_ms"] == 125


@pytest.mark.asyncio
async def test_qwen_realtime_surfaces_initialization_error_immediately() -> None:
    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session.created", "session": {}})
        await ws.receive()
        await ws.send_json(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_tools",
                    "message": "tool schema is invalid",
                },
            }
        )
        async for _ in ws:
            pass
        return ws

    runner, port = await _start_server(websocket)
    provider = QwenRealtimeProvider(
        f"ws://127.0.0.1:{port}/realtime",
        "secret-value",
        model="qwen3.5-omni-plus-realtime",
        connect_timeout=2,
        event_timeout=30,
    )
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(
            RuntimeError,
            match=r"tool schema is invalid .*invalid_request_error, invalid_tools",
        ):
            await provider.connect({"instructions": "identity", "tools": []})
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert asyncio.get_running_loop().time() - started < 2


@pytest.mark.asyncio
async def test_qwen_audio_realtime_uses_smart_turn_contract() -> None:
    observed: dict[str, Any] = {}

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        observed["model"] = request.query.get("model")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session.created", "session": {}})
        observed["session"] = json.loads((await ws.receive()).data)
        await ws.send_json({"type": "session.updated", "session": {}})
        async for _ in ws:
            pass
        return ws

    runner, port = await _start_server(websocket)
    provider = QwenRealtimeProvider(
        f"ws://127.0.0.1:{port}/realtime",
        "secret-value",
        model="qwen-audio-3.0-realtime-plus",
        voice="longanqian",
        connect_timeout=2,
        event_timeout=2,
    )
    try:
        await provider.connect({"instructions": "", "tools": []})
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert observed["model"] == "qwen-audio-3.0-realtime-plus"
    session = observed["session"]["session"]
    assert session["voice"] == "longanqian"
    assert session["turn_detection"] == {"type": "smart_turn"}
    assert "input_audio_format" not in session


@pytest.mark.asyncio
async def test_openai_realtime_ga_wire_contract_and_resampling() -> None:
    observed: dict[str, Any] = {}
    done = asyncio.Event()

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        observed["model"] = request.query.get("model")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        observed["session"] = json.loads((await ws.receive()).data)
        await ws.send_json({"type": "session.updated", "session": {}})
        observed["audio"] = json.loads((await ws.receive()).data)
        pcm = b"\x02\x00" * 240
        await ws.send_json(
            {"type": "response.created", "response": {"id": "openai-r1"}}
        )
        await ws.send_json(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(pcm).decode(),
            }
        )
        await ws.send_json(
            {
                "type": "response.output_audio_transcript.done",
                "event_id": "openai-e1",
                "transcript": "connected",
            }
        )
        await ws.send_json({"type": "response.done", "response": {}})
        done.set()
        async for _ in ws:
            pass
        return ws

    runner, port = await _start_server(websocket)
    provider = OpenAIRealtimeProvider(
        f"ws://127.0.0.1:{port}/realtime",
        "secret-value",
        model="gpt-realtime",
        connect_timeout=2,
        event_timeout=2,
    )
    audio: list[AudioDelta] = []

    async def on_audio(event: AudioDelta) -> None:
        audio.append(event)

    provider.on_audio_delta(on_audio)
    try:
        await provider.connect({"instructions": "identity", "tools": []})
        await provider.send_audio(b"\x00\x00" * 320)
        await asyncio.wait_for(done.wait(), timeout=2)
        await asyncio.sleep(0.05)
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert observed["model"] == "gpt-realtime"
    session = observed["session"]["session"]
    assert session["type"] == "realtime"
    assert session["audio"]["input"]["format"]["rate"] == 24000
    assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert len(base64.b64decode(observed["audio"]["audio"])) == 960
    assert audio[0].sample_rate == 24000
