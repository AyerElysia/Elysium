from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from .router.openai_router import OpenAIRouter

@register_plugin
class LiveBridgePlugin(BasePlugin):
    """直播桥接插件。

    提供 OpenAI 兼容的 API 接口，支持直播、STS2 与 Minecraft 操作AI桥接。
    """
    plugin_name = "Live-Bridge"
    plugin_description = "为直播框架、STS2 操作AI和 Minecraft 女仆 Agent 提供 OpenAI 兼容桥接"
    plugin_version = "1.2.0"

    def get_components(self) -> list[type]:
        return [OpenAIRouter]
