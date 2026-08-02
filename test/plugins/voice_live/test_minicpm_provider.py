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
