"""Voice Live 插件主类。

提供全双工实时语音通话能力，支持：
- 真全双工路径：OpenAI Realtime API / Moshi 协议
- 降级路径：MiMo-V2.5 多模态理解 + IndexTTS2 本地合成
"""

from __future__ import annotations

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .config import VoiceLiveConfig

logger = get_logger("voice_live", display="Voice Live", color="#89B4FA")


@register_plugin
class VoiceLivePlugin(BasePlugin):
    """全双工实时语音通话插件。

    架构：
    - VoiceLiveRouter: FastAPI 路由，提供 WebSocket 端点和静态页面
    - VoiceLiveEventHandler: 事件处理器，响应外部命令
    - VoiceLiveConsciousnessManager: 意识实例管理，与潜意识协同
    """

    plugin_name = "Voice-Live"
    plugin_description = "全双工实时语音通话"
    plugin_version = "1.0.0"
    configs = [VoiceLiveConfig]

    def __init__(self, config: VoiceLiveConfig | None = None) -> None:
        super().__init__(config)
        self.config: VoiceLiveConfig = config or VoiceLiveConfig()
        logger.info("Voice Live 插件初始化完成")

    def get_components(self) -> list[type]:
        from .event_handler import VoiceLiveEventHandler
        from .router import VoiceLiveRouter

        return [VoiceLiveRouter, VoiceLiveEventHandler]
