"""本地消息 TTS 插件入口。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin

from .actions.tts_action import TTSVoiceAction
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
        "本地消息文本转语音插件；当前部署使用 IndexTTS2 兼容服务"
    )
    plugin_version: str = "3.3.0"

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

        # 将自定义场景说明追加到 action 的描述，使 Chatter 侧感知使用时机
        if isinstance(self.config, TTSVoiceConfig):
            custom = self.config.prompt.custom_instructions.strip()
            if custom:
                base_description = TTSVoiceAction.action_description.split(
                    "\n\n自定义指令：", 1
                )[0].rstrip()
                TTSVoiceAction.action_description = (
                    base_description + "\n\n自定义指令：\n" + custom
                )
                try:
                    from src.app.plugin_system.api.action_api import clear_schema_cache

                    clear_schema_cache("tts_voice_plugin:action:tts_voice_action")
                except Exception as e:
                    logger.warning(f"清理 tts_voice_action schema 缓存失败: {e}")
                logger.debug("已将自定义场景说明追加到 tts_voice_action 描述")

    def get_components(self) -> list[type]:
        """返回插件内所有组件类。

        根据配置判断是否启用 Action 和 Command 组件。

        Returns:
            组件类列表
        """
        cfg: TTSVoiceConfig | None = self.config  # type: ignore[assignment]
        if not isinstance(cfg, TTSVoiceConfig) or not cfg.plugin.enable:
            logger.info("TTSVoicePlugin 已在本机配置中停用，不注册组件")
            return []

        from .services.tts_service import TTSService

        components: list[type] = [TTSService]
        action_enabled = cfg.components.action_enabled
        command_enabled = cfg.components.command_enabled

        if action_enabled:
            components.append(TTSVoiceAction)
        if command_enabled:
            components.append(TTSVoiceCommand)

        return components
