"""tts_voice_plugin 组件注册与退役契约测试。"""

from __future__ import annotations

from plugins.tts_voice_plugin.commands.tts_command import TTSVoiceCommand
from plugins.tts_voice_plugin.config import TTSVoiceConfig
from plugins.tts_voice_plugin.plugin import TTSVoicePlugin
from plugins.tts_voice_plugin.services.tts_service import TTSService


def test_disabled_tts_voice_plugin_registers_no_components() -> None:
    """plugin.enable=false 必须阻止本地 TTS 服务与组件注册。"""
    cfg = TTSVoiceConfig()
    cfg.plugin.enable = False

    assert TTSVoicePlugin(config=cfg).get_components() == []
    assert TTSVoicePlugin(config=None).get_components() == []


def test_tts_voice_plugin_registers_only_service_and_command() -> None:
    """已退役的 legacy Action 不再被插件注册；只保留 TTSService 与 TTSVoiceCommand。"""
    cfg = TTSVoiceConfig()
    cfg.plugin.enable = True
    cfg.components.command_enabled = True

    components = TTSVoicePlugin(config=cfg).get_components()
    assert components == [TTSService, TTSVoiceCommand]
