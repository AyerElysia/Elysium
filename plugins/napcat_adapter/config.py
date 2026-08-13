"""Napcat Adapter 配置定义"""
from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class NapcatAdapterConfig(BaseConfig):
    """Napcat 适配器配置"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Napcat/OneBot 11 适配器配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        """插件基本配置"""

        enabled: bool = Field(
            default=False,
            description="是否启用 Napcat 适配器",
            label="启用适配器",
            tag="plugin",
            order=0
        )
        config_version: str = Field(
            default="2.0.0",
            description="配置文件版本",
            label="配置版本",
            disabled=True,
            tag="general",
            order=1
        )

    @config_section("bot", title="Bot 配置", tag="user", order=10)
    class BotSection(SectionBase):
        """Bot 基本配置"""

        qq_id: str = Field(
            description="Bot 的 QQ 账号 ID",
            label="QQ 账号",
            placeholder="输入 Bot 的 QQ 号",
            tag="user",
            order=0
        )
        qq_nickname: str = Field(
            description="Bot 的 QQ 昵称",
            label="QQ 昵称",
            placeholder="输入 Bot 的昵称",
            tag="user",
            order=1
        )

    @config_section("napcat_server", title="Napcat 服务器", tag="network", order=20)
    class NapcatServerSection(SectionBase):
        """Napcat WebSocket 服务器配置"""

        mode: str = Field(
            default="reverse",
            description="ws 连接模式: reverse/direct",
            label="连接模式",
            input_type="select",
            choices=["reverse", "direct"],
            tag="network",
            hint="reverse: 逆向WebSocket; direct: 正向WebSocket",
            order=0
        )
        host: str = Field(
            default="localhost",
            description="Napcat WebSocket 服务地址",
            label="服务地址",
            placeholder="localhost",
            tag="network",
            order=1
        )
        port: int = Field(
            default=8095,
            description="Napcat WebSocket 服务端口",
            label="服务端口",
            ge=1,
            le=65535,
            tag="network",
            order=2
        )
        access_token: str = Field(
            default="",
            description="Napcat API 访问令牌（可选）",
            label="访问令牌",
            input_type="password",
            placeholder="可选，留空表示不鉴权",
            tag="security",
            order=3
        )

    @config_section("features", title="功能特性", tag="general", order=30)
    class FeaturesSection(SectionBase):
        """功能特性配置"""

        group_list_type: str = Field(
            default="blacklist",
            description="群聊名单模式: blacklist/whitelist",
            label="群聊名单模式",
            input_type="select",
            choices=["blacklist", "whitelist"],
            tag="list",
            order=0
        )
        group_list: list[str | int] = Field(
            default_factory=list,
            description="群聊名单；根据名单模式过滤",
            label="群聊名单",
            input_type="list",
            item_type="str",
            tag="list",
            hint="输入群号，根据上面的模式进行过滤",
            order=1
        )
        private_list_type: str = Field(
            default="blacklist",
            description="私聊名单模式: blacklist/whitelist",
            label="私聊名单模式",
            input_type="select",
            choices=["blacklist", "whitelist"],
            tag="list",
            order=2
        )
        private_list: list[str | int] = Field(
            default_factory=list,
            description="私聊名单；根据名单模式过滤",
            label="私聊名单",
            input_type="list",
            item_type="str",
            tag="list",
            hint="输入 QQ 号，根据上面的模式进行过滤",
            order=3
        )
        ban_user_id: list[str | int] = Field(
            default_factory=list,
            description="全局封禁的用户 ID 列表",
            label="封禁用户列表",
            input_type="list",
            item_type="str",
            tag="list",
            hint="这些用户的消息将被完全忽略",
            order=4
        )
        enable_poke: bool = Field(
            default=True,
            description="是否启用戳一戳消息处理",
            label="启用戳一戳",
            tag="general",
            order=5
        )
        ignore_non_self_poke: bool = Field(
            default=False,
            description="是否忽略不是针对自己的戳一戳消息",
            label="忽略非自己戳一戳",
            tag="general",
            depends_on="enable_poke",
            depends_value=True,
            order=6
        )
        poke_debounce_seconds: float = Field(
            default=2.0,
            description="戳一戳防抖时间（秒）",
            label="戳一戳防抖",
            ge=0.0,
            le=10.0,
            step=0.5,
            input_type="slider",
            tag="timer",
            depends_on="enable_poke",
            depends_value=True,
            order=7
        )
        enable_emoji_like: bool = Field(
            default=True,
            description="是否启用群聊表情回复处理",
            label="启用表情回复",
            tag="general",
            order=8
        )
        enable_reply_at: bool = Field(
            default=True,
            description="是否在回复时自动@原消息发送者",
            label="回复时@用户",
            tag="general",
            order=9
        )
        reply_at_rate: float = Field(
            default=0.5,
            description="回复时@的概率（0.0-1.0）",
            label="@概率",
            ge=0.0,
            le=1.0,
            step=0.05,
            input_type="slider",
            tag="performance",
            depends_on="enable_reply_at",
            depends_value=True,
            order=10
        )
        enable_video_processing: bool = Field(
            default=True,
            description="是否启用视频消息处理（下载和解析）",
            label="启用视频处理",
            tag="general",
            order=11
        )
        video_max_size_mb: int = Field(
            default=200,
            description="允许下载的视频文件最大大小（MB）",
            label="视频最大大小",
            ge=10,
            le=500,
            input_type="slider",
            tag="file",
            depends_on="enable_video_processing",
            depends_value=True,
            order=12
        )
        video_download_timeout: int = Field(
            default=60,
            description="视频下载超时时间（秒）",
            label="下载超时",
            ge=10,
            le=300,
            input_type="slider",
            tag="network",
            depends_on="enable_video_processing",
            depends_value=True,
            order=13
        )
        message_send_timeout_seconds: float = Field(
            default=20.0,
            description=(
                "等待 QQ NT 内核确认消息发送结果的最长时间；超时只表示投递状态未知，"
                "不会自动重发"
            ),
            label="消息发送确认超时",
            ge=5.0,
            le=60.0,
            step=1.0,
            input_type="slider",
            tag="network",
            order=14,
        )

    @config_section("events", title="事件感知", tag="general", order=40)
    class EventsSection(SectionBase):
        """事件感知配置"""

        enable_recall_notice: bool = Field(
            default=True,
            description="是否感知消息撤回事件",
            label="消息撤回感知",
            tag="notice",
            order=0
        )
        enable_member_change_notice: bool = Field(
            default=True,
            description="是否感知群成员变动（加入/退出/被踢）",
            label="成员变动感知",
            tag="notice",
            order=1
        )
        enable_admin_change_notice: bool = Field(
            default=True,
            description="是否感知管理员变动事件",
            label="管理员变动感知",
            tag="notice",
            order=2
        )
        enable_essence_notice: bool = Field(
            default=True,
            description="是否感知精华消息事件",
            label="精华消息感知",
            tag="notice",
            order=3
        )
        enable_request_notice: bool = Field(
            default=True,
            description="是否感知好友/群请求事件",
            label="请求事件感知",
            tag="request",
            order=4
        )

    @config_section("request_handling", title="请求处理", tag="general", order=50)
    class RequestHandlingSection(SectionBase):
        """请求事件处理策略"""

        friend_request_strategy: str = Field(
            default="notify",
            description="好友申请处理策略: auto_accept/ignore/notify",
            label="好友申请策略",
            input_type="select",
            choices=["auto_accept", "ignore", "notify"],
            tag="request",
            hint="auto_accept: 自动同意; ignore: 忽略; notify: 通知核心决策",
            order=0
        )
        group_add_strategy: str = Field(
            default="notify",
            description="加群申请处理策略: auto_accept/ignore/notify",
            label="加群申请策略",
            input_type="select",
            choices=["auto_accept", "ignore", "notify"],
            tag="request",
            order=1
        )
        group_invite_strategy: str = Field(
            default="notify",
            description="入群邀请处理策略: auto_accept/ignore/notify",
            label="入群邀请策略",
            input_type="select",
            choices=["auto_accept", "ignore", "notify"],
            tag="request",
            order=2
        )

    @config_section("identity", title="身份映射", tag="user", order=60)
    class IdentitySection(SectionBase):
        """Explicit QQ account to canonical-person mappings."""

        account_identity_aliases: list[str] = Field(
            default_factory=list,
            description=(
                "QQ 账号到跨平台人物键的显式映射，格式为 "
                "'QQ号=canonical_person_key'；禁止按昵称或消息内容自动合并人物"
            ),
            label="跨平台人物映射",
            input_type="list",
            item_type="str",
            tag="user",
            order=0,
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    bot: BotSection = Field(default_factory=BotSection)
    napcat_server: NapcatServerSection = Field(default_factory=NapcatServerSection)
    features: FeaturesSection = Field(default_factory=FeaturesSection)
    events: EventsSection = Field(default_factory=EventsSection)
    request_handling: RequestHandlingSection = Field(default_factory=RequestHandlingSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
