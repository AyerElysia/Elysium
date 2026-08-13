"""commands_plugin 入口。

统一收纳系统级聊天命令：
- CommandDispatchEventHandler：消息入站时拦截并执行命令
- ClearContextCommand：/清空上下文
- PermCommand：/perm 权限管理
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BasePlugin, register_plugin

from .commands.clear_command import ClearContextCommand
from .commands.perm_command import PermCommand
from .config import CommandsPluginConfig
from .dispatch import CommandDispatchEventHandler


@register_plugin
class CommandsPlugin(BasePlugin):
    """统一命令插件。"""

    plugin_name: str = "commands_plugin"
    plugin_description: str = "系统级聊天命令：命令分流、上下文清理、权限管理"
    plugin_version: str = "1.0.0"

    configs: ClassVar[list[type]] = [CommandsPluginConfig]
    dependent_components: ClassVar[list[str]] = []

    def __init__(self, config: CommandsPluginConfig | None = None) -> None:
        super().__init__(config)

    def get_components(self) -> list[type]:
        """返回插件组件列表。"""

        if not isinstance(self.config, CommandsPluginConfig):
            return []
        if not self.config.plugin.enabled:
            return []
        return [CommandDispatchEventHandler, ClearContextCommand, PermCommand]
