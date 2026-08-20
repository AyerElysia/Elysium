"""本地消息 TTS Service 契约测试。"""

from __future__ import annotations

import asyncio
import base64
import io
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.tts_voice_plugin import api as tts_api
from plugins.tts_voice_plugin.config import TTSVoiceConfig
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


def test_long_text_config_defaults_and_bounds() -> None:
    config = TTSVoiceConfig.model_validate({})

    assert config.tts.long_text_split_enabled is True
    assert config.tts.segment_max_units == 48
    assert config.tts.segment_min_units == 8
    assert config.tts.sentence_pause_ms == 320
    assert config.tts.paragraph_pause_ms == 520

    with pytest.raises(ValueError):
        TTSVoiceConfig.model_validate({"tts": {"segment_max_units": 15}})
    with pytest.raises(ValueError):
        TTSVoiceConfig.model_validate({"tts": {"sentence_pause_ms": -1}})


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
