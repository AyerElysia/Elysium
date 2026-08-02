from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from aiohttp import web

from plugins.voice_live.providers.base import AudioDelta, TranscriptEvent
from plugins.voice_live.providers.openai_realtime import OpenAIRealtimeProvider
from plugins.voice_live.providers.qwen_realtime import QwenRealtimeProvider


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
        observed["session"] = json.loads((await ws.receive()).data)
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
        await provider.connect({"instructions": "identity", "tools": []})
        await provider.send_audio(b"\x00\x00" * 320)
        await asyncio.wait_for(done.wait(), timeout=2)
        await asyncio.sleep(0.05)
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert observed["authorization"] == "Bearer secret-value"
    assert observed["model"] == "qwen3.5-omni-plus-realtime"
    session = observed["session"]["session"]
    assert session["turn_detection"] == {"type": "semantic_vad"}
    assert session["input_audio_format"] == "pcm"
    assert len(base64.b64decode(observed["audio"]["audio"])) == 640
    assert audio[0].sample_rate == 24000
    assert transcripts[-1] == TranscriptEvent("assistant", "已接通", True, "qwen-e1")


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
