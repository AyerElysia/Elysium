"""MiniCPM-o live 外部服务器桥接插件。"""

from __future__ import annotations

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .config import MiniCPMLiveBridgeConfig
from .event_handler import MiniCPMLiveUnifiedEventHandler
from .router import MiniCPMLiveRouter

logger = get_logger("minicpm_live_bridge")


@register_plugin
class MiniCPMLiveBridgePlugin(BasePlugin):
    """MiniCPM-o live external bridge.

    Neo 不负责下载或加载 MiniCPM-o 模型；外部服务器提供真正的 live 推理能力。
    """

    plugin_name = "MiniCPM-Live-Bridge"
    plugin_description = "MiniCPM-o live 外部服务器桥接"
    plugin_version = "0.1.0"
    configs = [MiniCPMLiveBridgeConfig]

    def __init__(self, config: MiniCPMLiveBridgeConfig | None = None) -> None:
        super().__init__(config)
        self.config: MiniCPMLiveBridgeConfig = config or MiniCPMLiveBridgeConfig()
        logger.info("MiniCPM Live Bridge 初始化完成")

    def get_components(self) -> list[type]:
        return [MiniCPMLiveRouter, MiniCPMLiveUnifiedEventHandler]
