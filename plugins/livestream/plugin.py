"""Replay-safe Bilibili livestream plugin."""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .config import LivestreamConfig

logger = get_logger("livestream", display="AI直播", color="#F5C2E7")


@register_plugin
class LivestreamPlugin(BasePlugin):
    """Manual control plane, unified director and acknowledged stage."""

    plugin_name = "Livestream"
    plugin_description = "可追溯的 B站直播导演、语音舞台与记忆闭环"
    plugin_version = "2.0.0"
    configs: ClassVar[list[type]] = [LivestreamConfig]

    def __init__(self, config: LivestreamConfig | None = None) -> None:
        super().__init__(config)
        self.config: LivestreamConfig = config or LivestreamConfig()
        logger.info("直播插件已装载；等待操作者手动开始")

    def get_components(self) -> list[type]:
        from .router import LivestreamRouter

        return [LivestreamRouter]
