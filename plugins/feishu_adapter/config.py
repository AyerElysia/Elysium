"""Feishu adapter configuration."""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


class FeishuAdapterConfig(BaseConfig):
    """Feishu self-built app adapter config."""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Feishu adapter config"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        """Plugin switches."""

        enabled: bool = Field(
            default=True,
            description="是否启用 Feishu 适配器",
            label="启用适配器",
            tag="plugin",
            order=0,
        )
        config_version: str = Field(
            default="0.1.0",
            description="配置文件版本",
            label="配置版本",
            disabled=True,
            tag="general",
            order=1,
        )

    @config_section("app", title="飞书应用", tag="network", order=10)
    class AppSection(SectionBase):
        """Feishu app credentials."""

        app_id: str = Field(
            default="",
            description="飞书自建应用的 App ID",
            label="App ID",
            placeholder="cli_xxx",
            tag="security",
            order=0,
        )
        app_secret: str = Field(
            default="",
            description="飞书自建应用的 App Secret",
            label="App Secret",
            input_type="password",
            tag="security",
            order=1,
        )
        verification_token: str = Field(
            default="",
            description="事件订阅 Verification Token；留空表示不校验",
            label="Verification Token",
            input_type="password",
            tag="security",
            order=2,
        )
        encrypt_key: str = Field(
            default="",
            description="事件订阅 Encrypt Key。当前 HTTP 回调版暂不解密，建议飞书后台先不要启用加密。",
            label="Encrypt Key",
            input_type="password",
            tag="security",
            order=3,
        )
        api_base_url: str = Field(
            default="https://open.feishu.cn",
            description="飞书开放平台 API 地址",
            label="API Base URL",
            tag="network",
            order=4,
        )

    @config_section("connection", title="连接方式", tag="network", order=15)
    class ConnectionSection(SectionBase):
        """How to receive Feishu events."""

        subscription_mode: str = Field(
            default="long_connection",
            description="事件订阅方式: long_connection/http_callback。没有公网域名时使用 long_connection。",
            label="订阅方式",
            input_type="select",
            choices=["long_connection", "http_callback"],
            tag="network",
            order=0,
        )
        auto_start_long_connection: bool = Field(
            default=True,
            description="订阅方式为 long_connection 时，适配器启动后自动连接飞书",
            label="自动启动长连接",
            tag="network",
            order=1,
        )
        long_connection_log_level: str = Field(
            default="WARNING",
            description="飞书 SDK 长连接日志等级；常规自动重连日志始终聚合且连接票据始终脱敏",
            label="长连接日志等级",
            input_type="select",
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            tag="network",
            order=2,
        )

    @config_section("bot", title="Bot 身份", tag="user", order=20)
    class BotSection(SectionBase):
        """Bot identity used by outgoing messages."""

        bot_open_id: str = Field(
            default="",
            description="Bot 的 open_id；可留空，仅用于过滤自身消息和发送历史显示",
            label="Bot Open ID",
            tag="user",
            order=0,
        )
        bot_name: str = Field(
            default="爱莉",
            description="Bot 名称",
            label="Bot 名称",
            tag="user",
            order=1,
        )

    @config_section("behavior", title="行为设置", tag="general", order=30)
    class BehaviorSection(SectionBase):
        """Adapter behavior."""

        reply_to_message: bool = Field(
            default=True,
            description="有 reply_to 时使用飞书消息回复接口，否则直接发送到会话",
            label="优先引用回复",
            tag="general",
            order=0,
        )
        ignore_bot_messages: bool = Field(
            default=True,
            description="忽略 sender_type=app 或 Bot open_id 发送的消息，避免自回环",
            label="忽略 Bot 自身消息",
            tag="general",
            order=1,
        )
        group_list_type: str = Field(
            default="blacklist",
            description="群聊名单模式: blacklist/whitelist",
            label="群聊名单模式",
            input_type="select",
            choices=["blacklist", "whitelist"],
            tag="list",
            order=2,
        )
        group_list: list[str] = Field(
            default_factory=list,
            description="飞书 chat_id 名单；按群聊名单模式过滤",
            label="群聊名单",
            input_type="list",
            item_type="str",
            tag="list",
            order=3,
        )
        private_list_type: str = Field(
            default="blacklist",
            description="私聊名单模式: blacklist/whitelist",
            label="私聊名单模式",
            input_type="select",
            choices=["blacklist", "whitelist"],
            tag="list",
            order=4,
        )
        private_list: list[str] = Field(
            default_factory=list,
            description="用户 open_id 名单；按私聊名单模式过滤",
            label="私聊名单",
            input_type="list",
            item_type="str",
            tag="list",
            order=5,
        )

    @config_section("identity", title="身份显示", tag="user", order=40)
    class IdentitySection(SectionBase):
        """Display-name aliases for Feishu users."""

        user_name_aliases: list[str] = Field(
            default_factory=list,
            description="飞书用户显示名映射，格式为 'open_id或union_id=昵称'",
            label="用户昵称映射",
            input_type="list",
            item_type="str",
            tag="user",
            order=0,
        )
        canonical_identity_aliases: list[str] = Field(
            default_factory=list,
            description=(
                "飞书账号到跨平台人物键的显式映射，格式为 "
                "'open_id或union_id=canonical_person_key'。人物键只表示确定的账号归属，"
                "不得通过昵称或消息内容自动推断"
            ),
            label="跨平台人物映射",
            input_type="list",
            item_type="str",
            tag="user",
            order=1,
        )
        resolve_display_names: bool = Field(
            default=True,
            description=(
                "没有配昵称映射时，调飞书通讯录/群成员接口把 open_id 换成真实昵称。"
                "关掉则回落为原始 ID"
            ),
            label="自动解析真实昵称",
            tag="user",
            order=2,
        )
        display_name_cache_ttl: float = Field(
            default=21600.0,
            description="昵称解析结果的缓存秒数，避免每条消息都打接口",
            label="昵称缓存时长(秒)",
            input_type="number",
            tag="user",
            ge=0.0,
            order=3,
        )
        display_name_negative_cache_ttl: float = Field(
            default=300.0,
            description=(
                "昵称查询失败结果的缓存秒数。失败缓存应短于成功缓存，"
                "确保补齐飞书权限后能够较快自动恢复"
            ),
            label="昵称失败缓存时长(秒)",
            input_type="number",
            tag="user",
            ge=0.0,
            order=4,
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    app: AppSection = Field(default_factory=AppSection)
    connection: ConnectionSection = Field(default_factory=ConnectionSection)
    bot: BotSection = Field(default_factory=BotSection)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
