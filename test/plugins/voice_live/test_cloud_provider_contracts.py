from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from aiohttp import web

from plugins.voice_live.protocol import ProviderState
from plugins.voice_live.providers.base import (
    AudioDelta,
    InterruptionEvent,
    TranscriptEvent,
)
from plugins.voice_live.providers.openai_realtime import OpenAIRealtimeProvider
from plugins.voice_live.providers.qwen_realtime import (
    _QWEN_CONTEXT_CHUNK_BYTES,
    _QWEN_MAX_FRAME_BYTES,
    _QWEN_WEBSOCKET_HEARTBEAT,
    QwenRealtimeProvider,
    _qwen_safe_tool_name,
    _split_utf8_text,
)


def test_qwen_realtime_does_not_require_upstream_pong() -> None:
    assert _QWEN_WEBSOCKET_HEARTBEAT is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["openai", "qwen"])
async def test_transient_context_is_deleted_and_completion_is_reported(
    provider_kind: str,
) -> None:
    """Realtime turn context leaves provider history after response.done."""

    if provider_kind == "openai":
        provider = OpenAIRealtimeProvider("ws://example/realtime", "secret")
    else:
        provider = QwenRealtimeProvider(
            "ws://example/realtime",
            "secret",
            model="qwen-realtime",
        )
    sent: list[dict[str, Any]] = []
    completions: list[bool] = []

    async def send(event: dict[str, Any]) -> None:
        sent.append(event)
        if event.get("type") == "conversation.item.create":
            await provider._handle_event(  # type: ignore[attr-defined]
                {
                    "type": "conversation.item.created",
                    "event_id": "context-accepted",
                    "item": dict(event["item"]),
                }
            )

    async def completed(success: bool) -> None:
        completions.append(success)

    provider._send = send  # type: ignore[method-assign]
    provider.on_response_done(completed)
    receipt = await provider.inject_context("transient-world")
    created_item = sent[0]["item"]["id"]
    assert receipt.exact is True
    assert receipt.transport_event_ids == ("context-accepted",)

    await provider._handle_event(  # type: ignore[attr-defined]
        {"type": "response.done", "response": {"status": "completed"}}
    )

    assert sent[-1] == {
        "type": "conversation.item.delete",
        "item_id": created_item,
    }
    assert completions == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["openai", "qwen"])
async def test_transient_context_receipt_rejects_changed_server_echo(
    provider_kind: str,
) -> None:
    if provider_kind == "openai":
        provider = OpenAIRealtimeProvider("ws://example/realtime", "secret")
    else:
        provider = QwenRealtimeProvider(
            "ws://example/realtime",
            "secret",
            model="qwen-realtime",
        )

    async def send(event: dict[str, Any]) -> None:
        item = dict(event["item"])
        item["content"] = [{"type": "input_text", "text": "changed"}]
        await provider._handle_event(  # type: ignore[attr-defined]
            {
                "type": "conversation.item.created",
                "event_id": "changed-echo",
                "item": item,
            }
        )

    provider._send = send  # type: ignore[method-assign]

    receipt = await provider.inject_context("expected")

    assert receipt.exact is False
    assert receipt.expected_sha256 != receipt.accepted_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["openai", "qwen"])
async def test_tool_result_ttl_survives_old_response_done_then_expires(
    provider_kind: str,
) -> None:
    """A tool result created by response N remains through N and expires after N+1."""

    if provider_kind == "openai":
        provider = OpenAIRealtimeProvider("ws://example/realtime", "secret")
    else:
        provider = QwenRealtimeProvider(
            "ws://example/realtime",
            "secret",
            model="qwen-audio-3.0-realtime-plus",
        )
    sent: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        sent.append(event)

    provider._send = send  # type: ignore[method-assign]
    provider._response_active = True  # type: ignore[attr-defined]
    await provider.submit_tool_result("call-1", "bounded-result")
    created = next(
        event
        for event in sent
        if event.get("type") == "conversation.item.create"
    )
    item_id = created["item"]["id"]

    await provider._handle_event(  # type: ignore[attr-defined]
        {"type": "response.done", "response": {"status": "completed"}}
    )
    assert not any(
        event.get("type") == "conversation.item.delete"
        and event.get("item_id") == item_id
        for event in sent
    )

    await provider._handle_event(  # type: ignore[attr-defined]
        {"type": "response.done", "response": {"status": "completed"}}
    )
    assert sent[-1] == {
        "type": "conversation.item.delete",
        "item_id": item_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_kind", ["openai", "qwen"])
async def test_frequent_tool_results_have_bounded_response_ttl(
    provider_kind: str,
) -> None:
    if provider_kind == "openai":
        provider = OpenAIRealtimeProvider("ws://example/realtime", "secret")
    else:
        provider = QwenRealtimeProvider(
            "ws://example/realtime",
            "secret",
            model="qwen-audio-3.0-realtime-plus",
        )
    sent: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        sent.append(event)

    provider._send = send  # type: ignore[method-assign]
    provider._response_active = True  # type: ignore[attr-defined]
    for index in range(64):
        await provider.submit_tool_result(f"call-{index}", f"result-{index}")

    assert len(provider._transient_context_expiry) == 64  # type: ignore[attr-defined]
    await provider._handle_event(  # type: ignore[attr-defined]
        {"type": "response.done", "response": {"status": "completed"}}
    )
    assert len(provider._transient_context_expiry) == 64  # type: ignore[attr-defined]
    await provider._handle_event(  # type: ignore[attr-defined]
        {"type": "response.done", "response": {"status": "completed"}}
    )
    assert provider._transient_context_expiry == {}  # type: ignore[attr-defined]
    assert sum(
        event.get("type") == "conversation.item.delete" for event in sent
    ) == 64


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
        await ws.send_json({"type": "response.created", "response": {"id": "qwen-r1"}})
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
        await ws.send_json(
            {"type": "response.done", "response": {"usage": {"total_tokens": 2}}}
        )
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
    assert (
        observed["sessions"][2]["session"]["tools"][0]["function"]["name"] == "remember"
    )
    assert len(base64.b64decode(observed["audio"]["audio"])) == 640
    assert audio[0].sample_rate == 24000
    assert transcripts[-1] == TranscriptEvent("assistant", "已接通", True, "qwen-e1")


def test_qwen_tool_name_mapping_is_reversible_and_protocol_safe() -> None:
    assert _qwen_safe_tool_name("action-report_state") == "action_report_state"


def test_qwen_context_chunking_preserves_unicode_below_frame_limit() -> None:
    text = "世界感知🌸" * 40_000

    chunks = _split_utf8_text(text, max_bytes=_QWEN_CONTEXT_CHUNK_BYTES)

    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(
        len(chunk.encode("utf-8")) <= _QWEN_CONTEXT_CHUNK_BYTES for chunk in chunks
    )


@pytest.mark.asyncio
async def test_qwen_context_events_stay_below_upstream_frame_limit() -> None:
    provider = QwenRealtimeProvider(
        "ws://127.0.0.1/realtime",
        "secret-value",
        model="qwen-audio-3.0-realtime-plus",
    )
    sent: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        event["event_id"] = f"voice_{'a' * 32}"
        sent.append(event)
        if event.get("type") == "conversation.item.create":
            await provider._handle_event(  # type: ignore[attr-defined]
                {
                    "type": "conversation.item.created",
                    "event_id": f"accepted-{len(sent)}",
                    "item": dict(event["item"]),
                }
            )

    provider._send = send  # type: ignore[method-assign]
    text = "跨场景上下文🌸" * 40_000

    receipt = await provider.inject_context(text)

    delivered = "".join(event["item"]["content"][0]["text"] for event in sent)
    assert delivered == text
    assert receipt.exact is True
    assert receipt.accepted_utf8_bytes == len(text.encode("utf-8"))
    assert all(
        len(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        <= _QWEN_MAX_FRAME_BYTES
        for event in sent
    )


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
    await asyncio.gather(
        provider.interrupt(played_audio_ms=125),
        provider.interrupt(played_audio_ms=125),
    )

    assert [event["type"] for event in websocket.events] == ["response.cancel"]
    assert provider._response_active is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_qwen_idle_speech_start_is_not_reported_as_barge_in() -> None:
    provider = QwenRealtimeProvider(
        "ws://127.0.0.1/realtime",
        "secret-value",
        model="qwen-audio-3.0-realtime-plus",
    )
    interruptions: list[Any] = []

    async def on_interruption(event: Any) -> None:
        interruptions.append(event)

    provider.on_interruption(on_interruption)

    await provider._handle_event(  # type: ignore[attr-defined]
        {"type": "input_audio_buffer.speech_started"}
    )
    assert interruptions == []

    provider._response_active = True  # type: ignore[attr-defined]
    provider._active_response_id = "response-1"  # type: ignore[attr-defined]
    provider._active_item_id = "item-1"  # type: ignore[attr-defined]
    await provider._handle_event(  # type: ignore[attr-defined]
        {"type": "input_audio_buffer.speech_started"}
    )

    assert interruptions == [
        InterruptionEvent("server_vad", "response-1", "item-1")
    ]


@pytest.mark.asyncio
async def test_qwen_omni_interrupt_truncates_only_once() -> None:
    class FakeWebSocket:
        closed = False

        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        async def send_str(self, payload: str) -> None:
            self.events.append(json.loads(payload))

    provider = QwenRealtimeProvider(
        "ws://127.0.0.1/realtime",
        "secret-value",
        model="qwen3.5-omni-plus-realtime",
    )
    websocket = FakeWebSocket()
    provider._ws = websocket  # type: ignore[assignment]
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
async def test_qwen_stale_cancel_error_is_recoverable() -> None:
    provider = QwenRealtimeProvider(
        "ws://127.0.0.1/realtime",
        "secret-value",
        model="qwen-audio-3.0-realtime-plus",
    )
    errors: list[str] = []
    metrics: list[dict[str, Any]] = []

    async def on_error(message: str) -> None:
        errors.append(message)

    async def on_metrics(event: Any) -> None:
        metrics.append(event.values)

    provider.on_error(on_error)
    provider.on_metrics(on_metrics)
    provider._interrupting_response_id = "response-1"  # type: ignore[attr-defined]
    provider._active_response_id = "response-1"  # type: ignore[attr-defined]
    await provider._emit_state(ProviderState.INTERRUPTED)

    await provider._handle_event(  # type: ignore[attr-defined]
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_value",
                "message": "Conversation has no active response.",
                "param": "response.cancel",
            },
        }
    )

    assert errors == []
    assert metrics[-1]["interruption"]["stale_cancel_ignored"] == 1
    assert provider._state is ProviderState.LISTENING  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_qwen_no_active_response_error_without_local_cancel_is_not_hidden() -> None:
    provider = QwenRealtimeProvider(
        "ws://127.0.0.1/realtime",
        "secret-value",
        model="qwen-audio-3.0-realtime-plus",
    )
    errors: list[str] = []

    async def on_error(message: str) -> None:
        errors.append(message)

    provider.on_error(on_error)
    await provider._handle_event(  # type: ignore[attr-defined]
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_value",
                "message": "Conversation has no active response.",
            },
        }
    )

    assert errors == [
        "Conversation has no active response. (invalid_request_error, invalid_value)"
    ]


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
        await provider.connect(
            {
                "instructions": "",
                "tools": [],
                "qwen_max_history_turns": 7,
            }
        )
    finally:
        await provider.disconnect()
        await runner.cleanup()

    assert observed["model"] == "qwen-audio-3.0-realtime-plus"
    session = observed["session"]["session"]
    assert session["voice"] == "longanqian"
    assert session["turn_detection"] == {"type": "smart_turn"}
    assert session["max_history_turns"] == 7
    assert session["input_audio_format"] == "pcm"
    assert session["output_audio_format"] == "pcm"


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
