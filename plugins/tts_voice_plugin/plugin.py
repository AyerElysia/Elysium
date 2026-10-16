"""本地消息 TTS 插件入口。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin

from .commands.tts_command import TTSVoiceCommand
from .config import TTSVoiceConfig

if TYPE_CHECKING:
    from .services.tts_service import TTSService

logger = get_logger("tts_voice_plugin")


@register_plugin
class TTSVoicePlugin(BasePlugin):
    """可配置的本地参考音频语音合成插件。"""

    plugin_name: str = "tts_voice_plugin"
    plugin_description: str = (
        "本地消息文本转语音插件；支持 GPT-SoVITS、IndexTTS2.5 与 Breeze TTS 2"
    )
    plugin_version: str = "3.4.0"

    configs = [TTSVoiceConfig]

    def __init__(self, config: TTSVoiceConfig | None = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置实例
        """
        super().__init__(config)
        self.tts_service: TTSService | None = None

    async def on_plugin_loaded(self) -> None:
        """插件加载后回调，初始化 TTS 服务。"""
        if not isinstance(self.config, TTSVoiceConfig) or not self.config.plugin.enable:
            logger.info("TTSVoicePlugin 已在本机配置中停用，跳过服务初始化")
            return
        logger.info("初始化 TTSVoicePlugin...")
        from .services.tts_service import TTSService

        self.tts_service = TTSService(self)
        logger.info("TTSService 已成功初始化。")

    async def on_plugin_unloaded(self) -> None:
        """Release only a TTS backend process started by this plugin instance."""
        service = self.tts_service
        self.tts_service = None
        if service is not None:
            await service.stop()

    def get_components(self) -> list[type]:
        """返回插件内所有组件类。

        Returns:
            组件类列表
        """
        cfg: TTSVoiceConfig | None = self.config  # type: ignore[assignment]
        if not isinstance(cfg, TTSVoiceConfig) or not cfg.plugin.enable:
            logger.info("TTSVoicePlugin 已在本机配置中停用，不注册组件")
            return []

        from .services.tts_service import TTSService

        components: list[type] = [TTSService]
        if cfg.components.command_enabled:
            components.append(TTSVoiceCommand)

        return components
