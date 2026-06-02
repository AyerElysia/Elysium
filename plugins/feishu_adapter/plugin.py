"""Feishu adapter plugin."""

from __future__ import annotations

from src.core.components.base.plugin import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.logger import get_logger

from .adapter import FeishuAdapter
from .config import FeishuAdapterConfig
from .router import FeishuRouter

logger = get_logger("feishu_adapter")


@register_plugin
class FeishuAdapterPlugin(BasePlugin):
    """把飞书自建应用接入统一消息流。"""

    plugin_name = "feishu_adapter"
    plugin_description = "Feishu self-built app adapter"
    plugin_version = "0.1.0"
    configs = [FeishuAdapterConfig]

    def __init__(self, config=None):
        super().__init__(config)
        logger.info("FeishuAdapterPlugin 初始化完成")

    def get_components(self) -> list[type]:
        return [FeishuRouter, FeishuAdapter]
