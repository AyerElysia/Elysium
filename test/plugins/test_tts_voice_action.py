"""tts_voice_plugin.actions.tts_action 行为测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.tts_voice_plugin.actions.tts_action import TTSVoiceAction
from plugins.tts_voice_plugin.config import TTSVoiceConfig
from plugins.tts_voice_plugin.plugin import TTSVoicePlugin


def test_disabled_tts_voice_plugin_registers_no_components() -> None:
    """plugin.enable=false 必须阻止旧 GPT-SoVITS 服务与 Action 注册。"""
    cfg = TTSVoiceConfig()
    cfg.plugin.enable = False

    assert TTSVoicePlugin(config=cfg).get_components() == []


def _build_action(*, always_available: bool, platform: str = "") -> TTSVoiceAction:
    cfg = TTSVoiceConfig()
    cfg.components.action_always_available = always_available

    plugin = SimpleNamespace(config=cfg, tts_service=None)
    chat_stream = SimpleNamespace(
        stream_id="s1",
        platform=platform,
        context=SimpleNamespace(history_messages=[]),
    )
    return TTSVoiceAction(chat_stream=chat_stream, plugin=plugin)


async def test_go_activate_returns_true_when_always_available_enabled(monkeypatch) -> None:
    """常驻可用开关开启时应直接激活，不走随机/关键词/LLM 判定。"""
    action = _build_action(always_available=True)

    async def _should_not_call(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("always_available=True 时不应调用动态激活分支")

    monkeypatch.setattr(action, "_random_activation", _should_not_call)
    monkeypatch.setattr(action, "_keyword_match", _should_not_call)
    monkeypatch.setattr(action, "_llm_judge_activation", _should_not_call)

    assert await action.go_activate() is True


async def test_go_activate_uses_keyword_when_always_available_disabled(monkeypatch) -> None:
    """关闭常驻可用后，命中关键词应激活。"""
    action = _build_action(always_available=False)
    action._last_message = "我想听你用语音说一句晚安"

    async def _random_false(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return False

    async def _llm_false(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return False

    monkeypatch.setattr(action, "_random_activation", _random_false)
    monkeypatch.setattr(action, "_llm_judge_activation", _llm_false)

    assert await action.go_activate() is True


async def test_go_activate_returns_false_when_all_conditions_miss(monkeypatch) -> None:
    """关闭常驻可用且无随机/关键词/LLM命中时应不激活。"""
    action = _build_action(always_available=False)
    action._last_message = "今天天气不错"

    async def _random_false(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return False

    async def _llm_false(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return False

    monkeypatch.setattr(action, "_random_activation", _random_false)
    monkeypatch.setattr(action, "_llm_judge_activation", _llm_false)

    assert await action.go_activate() is False


async def test_execute_persists_tts_text_as_voice_plain_text(monkeypatch) -> None:
    """TTS 语音消息应把合成文本写入 processed_plain_text。"""
    action = _build_action(always_available=True)

    class FakeTTSService:
        async def generate_voice(self, *, text: str, style_hint: str, language_hint: str | None) -> str:
            assert text == "今晚我想认真说一会儿。"
            assert style_hint == "gentle"
            assert language_hint == "zh"
            return "VOICE_BASE64"

    sent: dict[str, object] = {}

    async def fake_send_voice(**kwargs):  # type: ignore[no-untyped-def]
        sent.update(kwargs)
        return True

    action.tts_service = FakeTTSService()  # type: ignore[assignment]
    monkeypatch.setattr("plugins.tts_voice_plugin.actions.tts_action.send_voice", fake_send_voice)

    success, message = await action.execute(
        tts_voice_text="  今晚我想认真说一会儿。  ",
        voice_style="gentle",
        text_language="zh",
    )

    assert success is True
    assert "文本长度" in message
    assert sent == {
        "voice_data": "VOICE_BASE64",
        "stream_id": "s1",
        "processed_plain_text": "[语音:今晚我想认真说一会儿。]",
    }


async def test_surface_execute_is_suppressed_when_adapter_owns_tts() -> None:
    action = _build_action(always_available=True, platform="neko.surface")
    action.tts_service = SimpleNamespace()

    success, message = await action.execute(
        tts_voice_text="这条不应再生成第二份语音。",
    )

    assert success is False
    assert "自动 TTS" in message
