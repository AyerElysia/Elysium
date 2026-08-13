"""N.E.K.O presentation surface activation configuration."""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


class NekoSurfaceConfig(BaseConfig):
    """Keep every network-facing Surface component opt-in."""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "N.E.K.O Surface Gateway 配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=False,
            description="是否注册 N.E.K.O Surface 服务、路由与适配器",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
