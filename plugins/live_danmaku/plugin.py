"""Manual Bilibili danmaku page. Loading this plugin does not open a connection."""

from __future__ import annotations

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin


@register_plugin
class LiveDanmakuPlugin(BasePlugin):
    plugin_name = "live_danmaku"
    plugin_description = "手动连接 B 站房间并显示新弹幕"
    plugin_version = "0.1.0"

    def __init__(self, config=None) -> None:
        super().__init__(config)

    def get_components(self) -> list[type]:
        from .reply_handler import LiveDanmakuReplyHandler
        from .router import LiveDanmakuRouter

        return [LiveDanmakuRouter, LiveDanmakuReplyHandler]
