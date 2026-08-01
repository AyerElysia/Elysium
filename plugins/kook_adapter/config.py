"""KOOK 适配器配置定义"""
from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class KookAdapterConfig(BaseConfig):
    """KOOK 适配器配置"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "KOOK 平台适配器配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        """插件基本配置"""

        enabled: bool = Field(
            default=True,
            description="是否启用 KOOK 适配器",
            label="启用适配器",
            tag="plugin",
            order=0,
        )

    @config_section("bot", title="Bot 配置", tag="user", order=10)
    class BotSection(SectionBase):
        """Bot 凭据配置"""

        token: str = Field(
            default="",
            description="KOOK Bot Token（开发者中心获取）",
            label="Bot Token",
            input_type="password",
            placeholder="输入 KOOK Bot Token",
            tag="security",
            order=0,
        )
        bot_name: str = Field(
            default="",
            description="Bot 显示名称（用于日志和身份标识）",
            label="Bot 名称",
            placeholder="输入 Bot 名称",
            tag="user",
            order=1,
        )

    @config_section("features", title="功能特性", tag="general", order=20)
    class FeaturesSection(SectionBase):
        """功能配置"""

        channel_list_type: str = Field(
            default="blacklist",
            description="频道名单模式: blacklist/whitelist",
            label="频道名单模式",
            input_type="select",
            choices=["blacklist", "whitelist"],
            tag="list",
            order=0,
        )
        channel_list: list[str] = Field(
            default_factory=list,
            description="频道 ID 名单（根据模式过滤）",
            label="频道名单",
            input_type="list",
            item_type="str",
            tag="list",
            order=1,
        )
        enable_dm: bool = Field(
            default=True,
            description="是否接收私信消息",
            label="启用私信",
            tag="general",
            order=2,
        )
        reply_with_quote: bool = Field(
            default=True,
            description="回复时引用原消息",
            label="引用回复",
            tag="general",
            order=3,
        )
        use_kmarkdown: bool = Field(
            default=True,
            description="使用 KMarkdown 格式发送消息",
            label="KMarkdown 格式",
            tag="general",
            order=4,
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    bot: BotSection = Field(default_factory=BotSection)
    features: FeaturesSection = Field(default_factory=FeaturesSection)
