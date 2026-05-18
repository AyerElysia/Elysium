"""utility_commands — 实用命令集合插件。"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .commands.clear_command import ClearContextCommand

logger = get_logger("utility_commands")


@register_plugin
class UtilityCommandsPlugin(BasePlugin):
    """实用命令集合插件。"""

    plugin_name: str = "utility_commands"
    plugin_description: str = "实用命令集合插件，收纳常用运维/管理类命令"
    plugin_version: str = "1.0.0"

    configs: list[type] = []
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        """返回插件组件列表。"""
        return [ClearContextCommand]
