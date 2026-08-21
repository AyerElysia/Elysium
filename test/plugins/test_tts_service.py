"""本地消息 TTS Service 契约测试。"""

from __future__ import annotations

import asyncio
import base64
import io
import signal
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from plugins.tts_voice_plugin import api as tts_api
from plugins.tts_voice_plugin.config import TTSVoiceConfig
from plugins.tts_voice_plugin.plugin import TTSVoicePlugin
from plugins.tts_voice_plugin.services import tts_service as tts_service_module
from plugins.tts_voice_plugin.services.tts_service import TTSService


def _service_with_reference(path: Path) -> TTSService:
    config = TTSVoiceConfig()
    config.tts_styles[0].refer_wav_path = str(path)
    config.tts_styles[0].prompt_text = "参考文本"
    return TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]


def _silent_wav(*, duration_ms: int = 100, sample_rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * round(sample_rate * duration_ms / 1000))
    return output.getvalue()


class _FakeAudioContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int):
        yield self._body


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = body
        self.content = _FakeAudioContent(body)

    async def __aenter__(self) -> "_FakeHTTPResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def read(self) -> bytes:
        return self._body


class _FakeClientSession:
    last: "_FakeClientSession | None" = None
    response = _FakeHTTPResponse(_silent_wav())

    def __init__(self, **_kwargs: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        _FakeClientSession.last = self

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> _FakeHTTPResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


def test_long_text_config_defaults_and_bounds() -> None:
    config = TTSVoiceConfig.model_validate({})

    assert config.tts.backend == "legacy_compat"
    assert config.tts.model == "indextts25-timbre"
    assert config.tts.long_text_split_enabled is True
    assert config.tts.segment_max_units == 48
    assert config.tts.segment_min_units == 8
    assert config.tts.segment_concurrency == 2
    assert config.tts.sentence_pause_ms == 320
    assert config.tts.paragraph_pause_ms == 520

    with pytest.raises(ValueError):
        TTSVoiceConfig.model_validate({"tts": {"segment_max_units": 15}})
    with pytest.raises(ValueError):
        TTSVoiceConfig.model_validate({"tts": {"sentence_pause_ms": -1}})
    with pytest.raises(ValueError):
        TTSVoiceConfig.model_validate({"tts": {"segment_concurrency": 5}})
    with pytest.raises(ValueError):
        TTSVoiceConfig.model_validate({"tts": {"backend": "guessed"}})


def test_vllm_omni_payload_uses_official_indextts25_contract(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    config = TTSVoiceConfig()
    config.tts.backend = "vllm_omni"
    config.tts.model = "elysia-indextts25"
    config.tts_styles[0].refer_wav_path = str(reference)
    config.tts_styles[0].speed_factor = 1.05
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]

    payload = service._build_vllm_omni_payload(
        server_config=service.tts_styles["default"],
        text="你好，IndexTTS。",
        text_language="zh",
        response_format="wav",
        reference_audio_data_url="data:audio/wav;base64,UklGRg==",
    )

    assert payload == {
        "model": "elysia-indextts25",
        "input": "你好，IndexTTS。",
        "response_format": "wav",
        "speed": 1.05,
        "extra_params": {"lang": "zhen", "text_normalization": True},
        "ref_audio": "data:audio/wav;base64,UklGRg==",
    }
    assert "text" not in payload
    assert "text_lang" not in payload
    assert "ref_audio_path" not in payload
    assert "duration_factor" not in payload


def test_vllm_omni_named_voice_avoids_reference_retransmission(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    config = TTSVoiceConfig()
    config.tts.backend = "vllm_omni"
    config.tts_styles[0].refer_wav_path = str(reference)
    config.tts_styles[0].voice = "elysia-timbre"
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]

    payload = service._build_vllm_omni_payload(
        server_config=service.tts_styles["default"],
        text="命名音色测试。",
        text_language="zh",
        response_format="wav",
        reference_audio_data_url="data:audio/wav;base64,ignored",
    )

    assert payload["voice"] == "elysia-timbre"
    assert "ref_audio" not in payload


def test_indextts_reference_outside_legacy_window_is_not_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "long-reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    monkeypatch.setattr(service, "_probe_audio_duration", lambda _path: 18.5)

    assert service._validate_main_ref_duration(str(reference)) is True


def test_local_tts_resolver_prefers_plugin_owned_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(generate_voice=AsyncMock())
    manager = SimpleNamespace(
        get_plugin=lambda name: SimpleNamespace(tts_service=service)
        if name == "tts_voice_plugin"
        else None
    )
    fallback_called = False

    def fallback(_signature: str) -> object:
        nonlocal fallback_called
        fallback_called = True
        return None

    monkeypatch.setattr(tts_api, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(tts_api, "get_service", fallback)

    assert tts_api.get_local_tts_service() is service
    assert fallback_called is False


def test_local_tts_resolver_uses_framework_service_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(generate_voice=AsyncMock())
    signatures: list[str] = []
    manager = SimpleNamespace(get_plugin=lambda _name: None)

    def fallback(signature: str) -> object:
        signatures.append(signature)
        return service

    monkeypatch.setattr(tts_api, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(tts_api, "get_service", fallback)

    assert tts_api.get_local_tts_service() is service
    assert signatures == [tts_api.SERVICE_SIGNATURE]


async def test_server_auto_start_is_singleflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TTSVoiceConfig()
    config.tts.auto_start = True
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]
    started = False
    start_calls = 0

    async def is_alive(_server_url: str) -> bool:
        return started

    async def start(_server_url: str) -> bool:
        nonlocal start_calls, started
        start_calls += 1
        await asyncio.sleep(0.02)
        started = True
        return True

    monkeypatch.setattr(service, "_is_server_alive", is_alive)
    monkeypatch.setattr(service, "_start_server", start)

    results = await asyncio.gather(
        service._ensure_server_alive(config.tts.server),
        service._ensure_server_alive(config.tts.server),
    )

    assert results == [True, True]
    assert start_calls == 1


async def test_vllm_omni_call_uses_speech_endpoint_and_content_free_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    config = TTSVoiceConfig()
    config.tts.backend = "vllm_omni"
    config.tts.server = "http://127.0.0.1:8092"
    config.tts.model = "elysia-indextts25"
    config.tts_styles[0].refer_wav_path = str(reference)
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]
    service._ensure_server_alive = AsyncMock(return_value=True)  # type: ignore[method-assign]
    monkeypatch.setattr(tts_service_module.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        tts_service_module.aiohttp,
        "TCPConnector",
        lambda **_kwargs: object(),
    )

    result = await service._call_tts_api(
        service.tts_styles["default"],
        "一段测试。",
        "zh",
        request_media_type="wav",
        reference_audio_data_url="data:audio/wav;base64,UklGRg==",
        segment_index=1,
        segment_count=2,
    )

    assert result == _FakeClientSession.response._body
    assert _FakeClientSession.last is not None
    assert _FakeClientSession.last.calls == [
        {
            "url": "http://127.0.0.1:8092/v1/audio/speech",
            "json": {
                "model": "elysia-indextts25",
                "input": "一段测试。",
                "response_format": "wav",
                "speed": 1.0,
                "extra_params": {"lang": "zh", "text_normalization": True},
                "ref_audio": "data:audio/wav;base64,UklGRg==",
            },
            "headers": {"Content-Type": "application/json"},
        }
    ]


async def test_generate_voice_logs_no_private_synthesis_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    private_text = "这是一句不应该进入日志的私人语音正文"
    service._call_tts_api = AsyncMock(return_value=b"RIFF-local-audio")  # type: ignore[method-assign]
    messages: list[str] = []
    monkeypatch.setattr(
        tts_service_module.logger,
        "info",
        lambda message: messages.append(str(message)),
    )

    result = await service.generate_voice(private_text, style_hint="default")

    assert result == base64.b64encode(b"RIFF-local-audio").decode("utf-8")
    assert messages
    assert all(private_text not in message for message in messages)
    assert any("chars=" in message for message in messages)


def test_long_text_plan_preserves_order_and_semantic_boundaries(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    service._config.tts.segment_max_units = 16
    service._config.tts.segment_min_units = 4
    text = (
        "小星星，今天我想把路灯下的小光点讲给你听。"
        "它先躲在雨里，后来沿着水面慢慢靠近我们。"
        "最后，它停在掌心里说：我一直都在。"
    )

    segments = service._split_text_for_synthesis(text)

    assert len(segments) >= 3
    assert "".join(segment.text for segment in segments) == text
    assert all(0 < segment.units <= 16 for segment in segments)
    assert {segment.boundary for segment in segments} <= {
        "phrase",
        "clause",
        "sentence",
        "paragraph",
        "hard",
    }


def test_boundary_free_plan_is_linear_ordered_and_bounded(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    source = "爱" * 10_000

    segments = service._split_text_for_synthesis(source)

    assert "".join(segment.text for segment in segments) == source
    assert all(0 < segment.units <= service._config.tts.segment_max_units for segment in segments)


async def test_long_text_is_joined_into_one_complete_audio(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    service._config.tts.segment_max_units = 16
    service._config.tts.segment_min_units = 4
    service._config.tts_advanced.media_type = "wav"
    text = "第一句话会单独合成。第二句话也会合成。第三句话最后抵达。"
    calls: list[tuple[str, dict[str, object]]] = []

    async def synthesize(
        server_config: dict[str, object],
        text: str,
        text_language: str,
        **kwargs: object,
    ) -> bytes:
        del server_config, text_language
        calls.append((text, kwargs))
        return _silent_wav()

    service._call_tts_api = synthesize  # type: ignore[method-assign]

    encoded = await service.generate_voice(text)

    assert isinstance(encoded, str)
    audio = base64.b64decode(encoded, validate=True)
    with wave.open(io.BytesIO(audio), "rb") as joined:
        duration_ms = joined.getnframes() / joined.getframerate() * 1000
    plan = service._split_text_for_synthesis(service._clean_text_for_tts(text))
    expected_pause_ms = sum(
        service._pause_after_segment_ms(segment.boundary) for segment in plan[:-1]
    )
    assert duration_ms == pytest.approx(len(plan) * 100 + expected_pause_ms, abs=2)
    assert [call[0] for call in calls] == [segment.text for segment in plan]
    assert all(call[1]["request_media_type"] == "wav" for call in calls)
    assert all(call[1]["segment_count"] == len(plan) for call in calls)


async def test_vllm_long_text_batches_segments_but_joins_original_order(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    config = TTSVoiceConfig()
    config.tts.backend = "vllm_omni"
    config.tts.segment_max_units = 16
    config.tts.segment_min_units = 4
    config.tts.segment_concurrency = 2
    config.tts_styles[0].refer_wav_path = str(reference)
    config.tts_advanced.media_type = "wav"
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]
    active = 0
    maximum_active = 0
    completion_order: list[int] = []
    joined_order: list[bytes] = []
    transport_sessions: set[int] = set()

    async def synthesize(**kwargs: object) -> bytes:
        nonlocal active, maximum_active
        index = int(kwargs["segment_index"])
        assert kwargs["_server_ready"] is True
        transport_sessions.add(id(kwargs["_http_session"]))
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep({1: 0.04, 2: 0.01}.get(index, 0.0))
        completion_order.append(index)
        active -= 1
        return f"segment-{index}".encode()

    def join_in_original_order(
        audio_segments: list[bytes],
        _segments: list[object],
    ) -> bytes:
        joined_order.extend(audio_segments)
        return _silent_wav()

    service._call_tts_api = synthesize  # type: ignore[method-assign]
    service._join_wav_segments = join_in_original_order  # type: ignore[method-assign]
    service._ensure_server_alive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await service.generate_voice(
        "第一句话会单独合成。第二句话也会合成。第三句话最后抵达。"
    )

    assert result
    service._ensure_server_alive.assert_awaited_once()
    assert len(transport_sessions) == 1
    assert maximum_active == 2
    assert completion_order != sorted(completion_order)
    assert joined_order == [
        f"segment-{index}".encode() for index in range(1, len(joined_order) + 1)
    ]


async def test_long_text_failure_never_returns_partial_audio(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    service._config.tts.segment_max_units = 16
    service._config.tts.segment_min_units = 4
    service._config.tts_advanced.media_type = "wav"
    calls = 0

    async def fail_second(*_args: object, **_kwargs: object) -> bytes | None:
        nonlocal calls
        calls += 1
        return _silent_wav() if calls == 1 else None

    service._call_tts_api = fail_second  # type: ignore[method-assign]

    result = await service.generate_voice(
        "第一段已经成功，但第二段失败。第三段绝对不能被伪装成完整语音。"
    )

    assert result is None
    assert calls == 2


async def test_vllm_parallel_segment_failure_never_returns_partial_audio(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    config = TTSVoiceConfig()
    config.tts.backend = "vllm_omni"
    config.tts.segment_max_units = 16
    config.tts.segment_min_units = 4
    config.tts.segment_concurrency = 2
    config.tts_styles[0].refer_wav_path = str(reference)
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]

    async def fail_second(
        *_args: object,
        **kwargs: object,
    ) -> bytes | None:
        return None if int(kwargs["segment_index"]) == 2 else b"complete-segment"

    service._call_tts_api = fail_second  # type: ignore[method-assign]
    service._ensure_server_alive = AsyncMock(return_value=True)  # type: ignore[method-assign]
    joined = False

    def should_not_join(*_args: object, **_kwargs: object) -> bytes:
        nonlocal joined
        joined = True
        return _silent_wav()

    service._join_wav_segments = should_not_join  # type: ignore[method-assign]

    result = await service.generate_voice(
        "第一句话会成功。第二句话失败。第三句话不能被伪装成完整语音。"
    )

    assert result is None
    assert joined is False


async def test_total_text_limit_fails_without_silent_truncation(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    service._config.tts.max_text_length = 12
    service._call_tts_api = AsyncMock(return_value=_silent_wav())  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="max_text_length"):
        await service.generate_voice("这是一段绝对不能被静默截断后继续发送的完整表达。")

    service._call_tts_api.assert_not_awaited()


async def test_parallel_expressions_do_not_interleave_indextts_calls(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    active_calls = 0
    maximum_active_calls = 0

    async def synthesize(*_args: object, **_kwargs: object) -> bytes:
        nonlocal active_calls, maximum_active_calls
        active_calls += 1
        maximum_active_calls = max(maximum_active_calls, active_calls)
        await asyncio.sleep(0)
        active_calls -= 1
        return _silent_wav()

    service._call_tts_api = synthesize  # type: ignore[method-assign]

    first, second = await asyncio.gather(
        service.generate_voice("第一条并发表达。"),
        service.generate_voice("第二条并发表达。"),
    )

    assert first and second
    assert maximum_active_calls == 1


async def test_stop_reclaims_only_the_owned_server_process_group(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    process = SimpleNamespace(
        pid=731,
        returncode=None,
        wait=AsyncMock(return_value=0),
    )
    service._server_process = process  # type: ignore[assignment]

    with patch.object(tts_service_module.os, "killpg") as killpg:
        await service.stop()

    killpg.assert_called_once_with(731, signal.SIGTERM)
    process.wait.assert_awaited_once()
    assert service._server_process is None


async def test_stop_leaves_an_external_server_unaffected(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)

    with patch.object(tts_service_module.os, "killpg") as killpg:
        await service.stop()

    killpg.assert_not_called()


async def test_stop_cancellation_force_reclaims_owned_process_group(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    process = SimpleNamespace(
        pid=733,
        returncode=None,
        wait=AsyncMock(side_effect=[asyncio.CancelledError(), 0]),
    )
    service._server_process = process  # type: ignore[assignment]

    with patch.object(tts_service_module.os, "killpg") as killpg:
        with pytest.raises(asyncio.CancelledError):
            await service.stop()

    assert [call.args for call in killpg.call_args_list] == [
        (733, signal.SIGTERM),
        (733, signal.SIGKILL),
    ]
    assert process.wait.await_count == 2
    assert service._server_process is None


async def test_startup_timeout_reclaims_the_process_it_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    service._config.tts.server_dir = str(tmp_path)
    service._config.tts.start_command = "serve-local-tts"
    service._config.tts.startup_timeout = 1
    process = SimpleNamespace(
        pid=734,
        returncode=None,
        wait=AsyncMock(return_value=0),
        stderr=None,
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(tts_service_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tts_service_module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(service, "_is_server_alive", AsyncMock(return_value=False))

    with patch.object(tts_service_module.os, "killpg") as killpg:
        assert await service._start_server("http://127.0.0.1:8092") is False

    assert spawn.await_args.kwargs["start_new_session"] is True
    killpg.assert_called_once_with(734, signal.SIGTERM)
    process.wait.assert_awaited_once()
    assert service._server_process is None


async def test_cancelled_startup_reclaims_owned_process_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF")
    service = _service_with_reference(reference)
    service._config.tts.server_dir = str(tmp_path)
    service._config.tts.start_command = "serve-local-tts"
    process = SimpleNamespace(
        pid=735,
        returncode=None,
        wait=AsyncMock(return_value=0),
        stderr=None,
    )
    monkeypatch.setattr(
        tts_service_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        tts_service_module.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )

    with patch.object(tts_service_module.os, "killpg") as killpg:
        with pytest.raises(asyncio.CancelledError):
            await service._start_server("http://127.0.0.1:8092")

    killpg.assert_called_once_with(735, signal.SIGTERM)
    process.wait.assert_awaited_once()
    assert service._server_process is None


async def test_plugin_unload_stops_and_releases_owned_service() -> None:
    plugin = TTSVoicePlugin(TTSVoiceConfig())
    owned_service = SimpleNamespace(stop=AsyncMock())
    plugin.tts_service = owned_service  # type: ignore[assignment]

    await plugin.on_plugin_unloaded()

    owned_service.stop.assert_awaited_once()
    assert plugin.tts_service is None
