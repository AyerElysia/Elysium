"""Livestream 插件主类。

提供商业级 AI 直播能力：
- B站弹幕实时采集与互动
- 优先级调度（SC > 礼物 > 弹幕 > 进场）
- 主动行为引擎（空闲闲聊、话题切换、观众欢迎）
- TTS 语音合成 + Live2D 形象驱动
- 意识实例化，与潜意识协同
"""

from __future__ import annotations

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .config import LivestreamConfig

logger = get_logger("livestream", display="AI直播", color="#F5C2E7")


@register_plugin
class LivestreamPlugin(BasePlugin):
    """AI 直播插件。

    组件：
    - LivestreamRouter: FastAPI 路由，WebSocket + 静态页面
    - LivestreamEventHandler: 事件处理器，响应外部命令
    """

    plugin_name = "Livestream"
    plugin_description = "商业级 AI 直播框架（弹幕互动 + Live2D + TTS）"
    plugin_version = "1.0.0"
    configs = [LivestreamConfig]

    def __init__(self, config: LivestreamConfig | None = None) -> None:
        super().__init__(config)
        self.config: LivestreamConfig = config or LivestreamConfig()
        logger.info("AI 直播插件初始化完成")

    def get_components(self) -> list[type]:
        from .event_handler import LivestreamEventHandler
        from .router import LivestreamRouter

        return [LivestreamRouter, LivestreamEventHandler]
