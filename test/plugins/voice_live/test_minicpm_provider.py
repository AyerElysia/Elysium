from __future__ import annotations

import asyncio
import base64
import json
import struct
from typing import Any

import pytest
from aiohttp import web

from plugins.voice_live.providers.base import AudioDelta, TranscriptEvent
from plugins.voice_live.providers.minicpm_omni import MiniCPMOmniProvider


@pytest.mark.asyncio
async def test_minicpm_native_websocket_contract() -> None:
    observed: dict[str, Any] = {}
    server_done = asyncio.Event()

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        init = json.loads((await ws.receive()).data)
        observed["init"] = init
        await ws.send_json(
            {"type": "session.created", "session_id": "native", "mode": "full_duplex"}
        )
        input_event = json.loads((await ws.receive()).data)
        observed["input"] = input_event
        float_audio = b"".join(struct.pack("<f", 0.1) for _ in range(240))
        await ws.send_json(
            {
                "type": "response.output.delta",
                "kind": "audio",
                "response_id": "r1",
                "audio": base64.b64encode(float_audio).decode("ascii"),
                "metrics": {"decode_ms": 12},
            }
        )
        await ws.send_json(
            {
                "type": "response.output.delta",
                "kind": "text",
                "response_id": "r1",
                "text": "你",
            }
        )
        await ws.send_json(
            {
                "type": "response.done",
                "response_id": "r1",
                "text": "你好",
                "reason": "turn_end",
            }
        )
        server_done.set()
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/backend", websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    provider = MiniCPMOmniProvider(
        f"ws://127.0.0.1:{port}/backend",
        input_chunk_ms=20,
        connect_timeout=2,
        event_timeout=2,
    )
    audio: list[AudioDelta] = []
    transcripts: list[TranscriptEvent] = []
    metrics: list[dict[str, Any]] = []

    async def on_audio(event: AudioDelta) -> None:
        audio.append(event)

    async def on_transcript(event: TranscriptEvent) -> None:
        transcripts.append(event)

    async def on_metrics(event: Any) -> None:
        metrics.append(event.values)

    provider.on_audio_delta(on_audio)
    provider.on_transcript(on_transcript)
    provider.on_metrics(on_metrics)
    try:
        await provider.connect({"instructions": "identity", "provider_config": {}})
        await provider.send_audio(b"\x00\x00" * 320)
        await asyncio.wait_for(server_done.wait(), timeout=2)
        await asyncio.sleep(0.05)
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert observed["init"]["type"] == "session.init"
    assert observed["init"]["payload"]["system_prompt"] == "identity"
    raw_float = base64.b64decode(observed["input"]["input"]["audio_base64"])
    assert len(raw_float) == 320 * 4
    assert audio and len(audio[0].data) == 240 * 2 and audio[0].sample_rate == 24000
    assert transcripts[-1] == TranscriptEvent("assistant", "你好", True, "r1")
    assert metrics == [{"decode_ms": 12}]


@pytest.mark.asyncio
async def test_minicpm_initialization_error_fails_connect_immediately() -> None:
    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive()
        await ws.send_json({"type": "error", "message": "active session exists"})
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/backend", websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    provider = MiniCPMOmniProvider(
        f"ws://127.0.0.1:{port}/backend",
        connect_timeout=2,
        event_timeout=30,
    )
    try:
        with pytest.raises(RuntimeError, match="active session exists"):
            await asyncio.wait_for(provider.connect({}), timeout=2)
    finally:
        await provider.disconnect()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_minicpm_turn_audio_uses_native_turn_based_shape() -> None:
    observed: dict[str, Any] = {}
    server_done = asyncio.Event()

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        observed["init"] = json.loads((await ws.receive()).data)
        await ws.send_json({"type": "session.created", "session_id": "turn"})
        observed["input"] = json.loads((await ws.receive()).data)
        audio = base64.b64encode(struct.pack("<f", 0.2) * 240).decode("ascii")
        await ws.send_json(
            {
                "type": "response.done",
                "response_id": "turn-1",
                "text": "收到",
                "audio": audio,
            }
        )
        server_done.set()
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/backend", websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    provider = MiniCPMOmniProvider(
        f"ws://127.0.0.1:{port}/backend",
        mode="turn_based",
        connect_timeout=2,
        event_timeout=2,
    )
    try:
        await provider.connect({"provider_config": {"generation": {"max_new_tokens": 64}}})
        await provider.send_turn_audio(b"\x01\x00" * 320)
        await asyncio.wait_for(server_done.wait(), timeout=2)
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert observed["init"]["payload"]["mode"] == "turn_based"
    turn_input = observed["input"]["input"]
    assert turn_input["streaming"] is True
    assert turn_input["use_tts_template"] is True
    assert turn_input["generation"] == {"max_new_tokens": 64}
    content = turn_input["messages"][0]["content"][0]
    assert content["type"] == "audio"
    assert len(base64.b64decode(content["data"])) == 320 * 4
