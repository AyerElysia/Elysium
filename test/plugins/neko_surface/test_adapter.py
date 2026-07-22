from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from typing import Any
import wave

import pytest

from plugins.neko_surface.adapter import NekoSurfaceAdapter, PLATFORM, set_neko_surface_adapter
from plugins.neko_surface.protocol import SurfaceEvent
from src.core.models.message import MessageType
from src.core.transport.message_receive.converter import MessageConverter


class _DummySink:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class _FakeGateway:
    def __init__(self) -> None:
        self.handler = None
        self.events: list[tuple[str, dict[str, Any]]] = []

    def bind_input_handler(self, handler) -> None:
        self.handler = handler

    async def publish(self, event_type: str, **kwargs: Any) -> int:
        self.events.append((event_type, kwargs))
        return 1


class _FakeTTSService:
    def __init__(self, audio: str = "YQ==") -> None:
        self.audio = audio
        self.calls: list[tuple[str, str]] = []
        self._config = SimpleNamespace(
            higgs_cloud=SimpleNamespace(response_format="mp3"),
        )

    async def generate_voice(self, text: str, style: str) -> str:
        self.calls.append((text, style))
        return self.audio


def _wav_audio() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * 1_600)
    return output.getvalue()


def _jpeg_image() -> bytes:
    return b"\xff\xd8\xff\xe0surface-jpeg"


def test_tts_mime_type_uses_media_type_for_non_higgs_engine() -> None:
    service = SimpleNamespace(
        _config=SimpleNamespace(
            tts=SimpleNamespace(engine="gpt_sovits"),
            tts_advanced=SimpleNamespace(media_type="wav"),
        )
    )

    assert NekoSurfaceAdapter._tts_mime_type(service) == "audio/wav"


def test_surface_tts_text_is_split_at_sentence_boundaries() -> None:
    assert NekoSurfaceAdapter._split_surface_tts_text(
        "第一句。第二句！第三句？"
    ) == ["第一句。", "第二句！", "第三句？"]


def test_surface_tts_text_caps_long_punctuation_free_runs() -> None:
    text = "甲" * 190
    segments = NekoSurfaceAdapter._split_surface_tts_text(text)
    assert len(segments) == 2
    assert "".join(segments) == text


@pytest.mark.asyncio
async def test_user_text_enters_core_sink_as_canonical_message() -> None:
    sink = _DummySink()
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(sink, plugin=SimpleNamespace(gateway=gateway))
    event = SurfaceEvent.create(
        "user.text",
        event_id="event-1",
        sequence=2,
        session_id="session-1",
        surface_id="neko-main",
        character="Elysia",
        origin="neko",
        payload={"text": "Good morning", "user_id": "owner-1", "user_name": "Owner"},
    )

    try:
        await adapter.handle_surface_event(event)
    finally:
        set_neko_surface_adapter(None)

    assert len(sink.messages) == 1
    envelope = sink.messages[0]
    assert envelope["direction"] == "incoming"
    assert envelope["message_info"]["platform"] == PLATFORM
    assert envelope["message_info"]["user_info"]["user_id"] == "owner-1"
    assert envelope["message_segment"] == [{"type": "text", "data": "Good morning"}]
    assert envelope["metadata"]["surface_id"] == "neko-main"
    assert envelope["message_info"]["extra"]["bypass_message_buffer"] is True


@pytest.mark.asyncio
async def test_user_audio_enters_core_as_validated_voice_attachment() -> None:
    sink = _DummySink()
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(sink, plugin=SimpleNamespace(gateway=gateway))
    wav_audio = _wav_audio()
    event = SurfaceEvent.create(
        "user.audio",
        event_id="audio-event-1",
        sequence=3,
        session_id="session-1",
        turn_id="audio-turn-1",
        surface_id="neko-main",
        character="Elysia",
        origin="neko",
        payload={
            "data": base64.b64encode(wav_audio).decode("ascii"),
            "mime_type": "audio/wav",
            "duration_seconds": 0.1,
            "user_id": "owner-1",
            "user_name": "Owner",
        },
    )

    try:
        await adapter.handle_surface_event(event)
    finally:
        set_neko_surface_adapter(None)

    assert len(sink.messages) == 1
    envelope = sink.messages[0]
    assert envelope["message_segment"][0]["type"] == "voice"
    assert envelope["message_segment"][0]["data"]["mime_type"] == "audio/wav"
    assert envelope["metadata"]["event_type"] == "user.audio"
    assert envelope["metadata"]["bypass_message_buffer"] is True

    message = await MessageConverter().envelope_to_message(envelope)
    assert message.message_type is MessageType.VOICE
    assert message.processed_plain_text == "[语音]"
    assert len(message.attachments) == 1
    assert message.attachments[0].media_ref.mime_type == "audio/wav"
    assert message.attachments[0].media_ref.data == wav_audio


@pytest.mark.asyncio
async def test_user_screen_enters_core_as_proactive_image_attachment() -> None:
    sink = _DummySink()
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(sink, plugin=SimpleNamespace(gateway=gateway))
    image = _jpeg_image()
    event = SurfaceEvent.create(
        "user.screen",
        event_id="screen-event-1",
        sequence=4,
        session_id="session-1",
        turn_id="screen-turn-1",
        surface_id="neko-main",
        character="Elysia",
        origin="neko",
        payload={
            "data": base64.b64encode(image).decode("ascii"),
            "mime_type": "image/jpeg",
            "window_title": "Visual Studio Code",
            "capture_type": "periodic",
            "enabled_modes": ["vision", "window"],
        },
    )

    try:
        await adapter.handle_surface_event(event)
    finally:
        set_neko_surface_adapter(None)

    assert len(sink.messages) == 1
    envelope = sink.messages[0]
    assert envelope["message_segment"][0]["type"] == "text"
    assert "N.E.K.O" in envelope["message_segment"][0]["data"]
    assert envelope["message_segment"][1]["type"] == "image"
    assert envelope["message_segment"][1]["data"]["mime_type"] == "image/jpeg"
    assert envelope["metadata"]["is_proactive_opportunity_trigger"] is True
    assert envelope["metadata"]["is_proactive_vision"] is True
    assert envelope["metadata"]["proactive"] is True
    assert envelope["metadata"]["window_title"] == "Visual Studio Code"
    assert envelope["metadata"]["capture_type"] == "periodic"
    assert envelope["metadata"]["enabled_modes"] == ["vision", "window"]
    assert envelope["metadata"]["bypass_message_buffer"] is True

    message = await MessageConverter().envelope_to_message(envelope)
    assert message.message_type is MessageType.IMAGE
    assert message.processed_plain_text is not None
    assert "Visual Studio Code" in message.processed_plain_text
    assert "[图片]" in message.processed_plain_text
    assert len(message.attachments) == 1
    assert message.attachments[0].media_ref.mime_type == "image/jpeg"
    assert message.attachments[0].media_ref.data == image
    assert message.extra["is_proactive_opportunity_trigger"] is True
    assert message.extra["is_proactive_vision"] is True


@pytest.mark.asyncio
async def test_outgoing_message_without_tts_service_stays_text_only(monkeypatch) -> None:
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(
        _DummySink(),
        plugin=SimpleNamespace(gateway=gateway),
    )
    monkeypatch.setattr(
        NekoSurfaceAdapter,
        "_get_tts_service",
        staticmethod(lambda: None),
    )

    try:
        await adapter._send_platform_message(
            {
                "direction": "outgoing",
                "message_info": {"message_id": "reply-1", "platform": PLATFORM},
                "message_segment": [{"type": "text", "data": "Here I am."}],
            }
        )
    finally:
        set_neko_surface_adapter(None)

    assert [event_type for event_type, _ in gateway.events] == [
        "assistant.text",
        "turn.end",
    ]
    assert gateway.events[0][1]["payload"]["text"] == "Here I am."
    assert gateway.events[0][1]["turn_id"] == "reply-1"


@pytest.mark.asyncio
async def test_outgoing_surface_text_gets_one_automatic_higgs_voice(monkeypatch) -> None:
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(
        _DummySink(),
        plugin=SimpleNamespace(gateway=gateway),
    )
    service = _FakeTTSService()
    monkeypatch.setattr(
        NekoSurfaceAdapter,
        "_get_tts_service",
        staticmethod(lambda: service),
    )

    try:
        await adapter._send_platform_message(
            {
                "direction": "outgoing",
                "message_info": {"message_id": "reply-tts-1", "platform": PLATFORM},
                "message_segment": [{"type": "text", "data": "Here I am."}],
            }
        )
        tts_task = adapter._tts_tail
        assert tts_task is not None
        await tts_task
    finally:
        set_neko_surface_adapter(None)

    assert service.calls == [("Here I am.", "default")]
    assert [event_type for event_type, _ in gateway.events] == [
        "assistant.text",
        "turn.end",
        "assistant.voice",
    ]
    voice_payload = gateway.events[2][1]["payload"]
    assert voice_payload["data"] == "YQ=="
    assert voice_payload["mime_type"] == "audio/mpeg"
    assert voice_payload["speech_id"] == "reply-tts-1"


@pytest.mark.asyncio
async def test_outgoing_surface_text_streams_sentence_voices_in_order(monkeypatch) -> None:
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(
        _DummySink(),
        plugin=SimpleNamespace(gateway=gateway),
    )
    service = _FakeTTSService()
    monkeypatch.setattr(
        NekoSurfaceAdapter,
        "_get_tts_service",
        staticmethod(lambda: service),
    )

    try:
        await adapter._send_platform_message(
            {
                "direction": "outgoing",
                "message_info": {"message_id": "reply-tts-split", "platform": PLATFORM},
                "message_segment": [
                    {"type": "text", "data": "第一句。第二句！"},
                ],
            }
        )
        tts_task = adapter._tts_tail
        assert tts_task is not None
        await tts_task
    finally:
        await adapter.on_adapter_unloaded()

    assert service.calls == [("第一句。", "default"), ("第二句！", "default")]
    voice_events = [
        kwargs for event_type, kwargs in gateway.events if event_type == "assistant.voice"
    ]
    assert [item["payload"]["speech_id"] for item in voice_events] == [
        "reply-tts-split",
        "reply-tts-split",
    ]


@pytest.mark.asyncio
async def test_slow_surface_tts_does_not_block_text_delivery(monkeypatch) -> None:
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(
        _DummySink(),
        plugin=SimpleNamespace(gateway=gateway),
    )
    service = _FakeTTSService()
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_voice(text: str, style: str) -> str:
        service.calls.append((text, style))
        started.set()
        await release.wait()
        return service.audio

    monkeypatch.setattr(service, "generate_voice", delayed_voice)
    monkeypatch.setattr(
        NekoSurfaceAdapter,
        "_get_tts_service",
        staticmethod(lambda: service),
    )

    try:
        await asyncio.wait_for(
            adapter._send_platform_message(
                {
                    "direction": "outgoing",
                    "message_info": {"message_id": "reply-slow-1", "platform": PLATFORM},
                    "message_segment": [{"type": "text", "data": "I am already here."}],
                }
            ),
            timeout=0.1,
        )
        assert [event_type for event_type, _ in gateway.events] == [
            "assistant.text",
            "turn.end",
        ]

        await asyncio.wait_for(started.wait(), timeout=0.1)
        tts_task = adapter._tts_tail
        assert tts_task is not None and not tts_task.done()
        release.set()
        await tts_task
    finally:
        await adapter.on_adapter_unloaded()

    assert [event_type for event_type, _ in gateway.events] == [
        "assistant.text",
        "turn.end",
        "assistant.voice",
    ]


@pytest.mark.asyncio
async def test_split_reply_synthesizes_in_parallel_but_publishes_voice_in_order(
    monkeypatch,
) -> None:
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(
        _DummySink(),
        plugin=SimpleNamespace(gateway=gateway),
    )
    service = _FakeTTSService()
    both_started = asyncio.Event()
    releases = {
        "First paragraph": asyncio.Event(),
        "Second paragraph": asyncio.Event(),
    }

    async def controlled_voice(text: str, style: str) -> str:
        service.calls.append((text, style))
        if len(service.calls) == 2:
            both_started.set()
        await releases[text].wait()
        return "MQ==" if text == "First paragraph" else "Mg=="

    monkeypatch.setattr(service, "generate_voice", controlled_voice)
    monkeypatch.setattr(
        NekoSurfaceAdapter,
        "_get_tts_service",
        staticmethod(lambda: service),
    )

    try:
        await adapter._send_platform_message(
            {
                "direction": "outgoing",
                "message_info": {"message_id": "reply-part-1", "platform": PLATFORM},
                "message_segment": [{"type": "text", "data": "First paragraph"}],
            }
        )
        await adapter._send_platform_message(
            {
                "direction": "outgoing",
                "message_info": {"message_id": "reply-part-2", "platform": PLATFORM},
                "message_segment": [{"type": "text", "data": "Second paragraph"}],
            }
        )
        tail = adapter._tts_tail
        assert tail is not None
        await asyncio.wait_for(both_started.wait(), timeout=0.1)

        releases["Second paragraph"].set()
        await asyncio.sleep(0)
        assert "assistant.voice" not in [event_type for event_type, _ in gateway.events]

        releases["First paragraph"].set()
        await tail
    finally:
        await adapter.on_adapter_unloaded()

    assert service.calls == [
        ("First paragraph", "default"),
        ("Second paragraph", "default"),
    ]
    assert [event_type for event_type, _ in gateway.events] == [
        "assistant.text",
        "turn.end",
        "assistant.text",
        "turn.end",
        "assistant.voice",
        "assistant.voice",
    ]
    assert gateway.events[4][1]["payload"]["speech_id"] == "reply-part-1"
    assert gateway.events[5][1]["payload"]["speech_id"] == "reply-part-2"


@pytest.mark.asyncio
async def test_new_user_turn_cancels_old_surface_tts(monkeypatch) -> None:
    sink = _DummySink()
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(
        sink,
        plugin=SimpleNamespace(gateway=gateway),
    )
    service = _FakeTTSService()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_finishes(text: str, style: str) -> str:
        service.calls.append((text, style))
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(service, "generate_voice", never_finishes)
    monkeypatch.setattr(
        NekoSurfaceAdapter,
        "_get_tts_service",
        staticmethod(lambda: service),
    )

    try:
        await adapter._send_platform_message(
            {
                "direction": "outgoing",
                "message_info": {"message_id": "old-reply", "platform": PLATFORM},
                "message_segment": [{"type": "text", "data": "Old reply"}],
            }
        )
        await asyncio.wait_for(started.wait(), timeout=0.1)
        old_task = adapter._tts_tail
        assert old_task is not None

        await adapter.handle_surface_event(
            SurfaceEvent.create(
                "user.text",
                event_id="new-user-turn",
                sequence=3,
                session_id="session-1",
                surface_id="neko-main",
                character="Elysia",
                origin="neko",
                payload={"text": "Stop and listen"},
            )
        )
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
        await asyncio.gather(old_task, return_exceptions=True)
    finally:
        await adapter.on_adapter_unloaded()

    assert len(sink.messages) == 1
    assert [event_type for event_type, _ in gateway.events] == [
        "assistant.text",
        "turn.end",
    ]


@pytest.mark.asyncio
async def test_explicit_voice_segment_is_not_auto_synthesized(monkeypatch) -> None:
    gateway = _FakeGateway()
    adapter = NekoSurfaceAdapter(
        _DummySink(),
        plugin=SimpleNamespace(gateway=gateway),
    )
    service = _FakeTTSService()
    monkeypatch.setattr(
        NekoSurfaceAdapter,
        "_get_tts_service",
        staticmethod(lambda: service),
    )

    try:
        await adapter._send_platform_message(
            {
                "direction": "outgoing",
                "message_info": {"message_id": "reply-voice-1", "platform": PLATFORM},
                "message_segment": [
                    {"type": "text", "data": "Here I am."},
                    {"type": "voice", "data": "QkI=", "mime_type": "audio/mpeg"},
                ],
            }
        )
    finally:
        set_neko_surface_adapter(None)

    assert service.calls == []
    assert [event_type for event_type, _ in gateway.events] == [
        "assistant.text",
        "assistant.voice",
        "turn.end",
    ]
    assert gateway.events[1][1]["payload"]["data"] == "QkI="
