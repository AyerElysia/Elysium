"""Configuration for the Ayla independent-application channel."""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


class AylaAdapterConfig(BaseConfig):
    """Ayla adapter configuration.

    Ayla inbound traffic uses the authenticated application injection API and
    delivered replies are projected by Ayla's SSE bridge.  The adapter owns no
    network connection; it exists so the common outbound transport can record
    an acknowledgement without creating a second delivery channel.
    """

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Ayla 独立应用聊天通道配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=True,
            description="是否注册 Ayla 应用通道适配器",
            label="启用适配器",
            tag="plugin",
            order=0,
        )
        config_version: str = Field(
            default="1.0.0",
            description="配置文件版本",
            label="配置版本",
            disabled=True,
            tag="general",
            order=1,
        )

    @config_section("backend", title="Ayla 应用", tag="general", order=10)
    class BackendSection(SectionBase):
        backend_url: str = Field(
            default="",
            description=(
                "Ayla 后端地址，仅作部署元数据；当前出站由 SSE 投影，适配器不主动请求"
            ),
            label="后端地址",
            placeholder="http://127.0.0.1:18080",
            order=0,
        )
        bot_name: str = Field(
            default="爱莉",
            description="Ayla 通道展示的 Bot 名称",
            label="Bot 名称",
            order=1,
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    backend: BackendSection = Field(default_factory=BackendSection)
