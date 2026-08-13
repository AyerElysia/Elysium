"""commands_plugin activation configuration."""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base import BaseConfig
from src.kernel.config.core import Field, SectionBase, config_section


class CommandsPluginConfig(BaseConfig):
    """Keep context and permission mutations explicitly opt-in."""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "系统级聊天命令配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=False,
            description="是否注册上下文清理与权限管理命令",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
