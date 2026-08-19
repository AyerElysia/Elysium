"""本地消息 TTS Service 契约测试。"""

from __future__ import annotations

import asyncio
import base64
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
