"""桌宠桥接插件。"""

from __future__ import annotations

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .adapter import DesktopPetAdapter
from .router import DesktopPetRouter

logger = get_logger("desktop_pet")


@register_plugin
class DesktopPetPlugin(BasePlugin):
    """把本地桌宠作为统一主意识的一个外部身体/聊天通道。"""

    plugin_name = "desktop_pet"
    plugin_description = "本地桌宠桥接"
    plugin_version = "0.1.0"

    def __init__(self, config=None):
        super().__init__(config)
        logger.info("DesktopPetPlugin 初始化完成")

    def get_components(self) -> list[type]:
        return [DesktopPetRouter, DesktopPetAdapter]
