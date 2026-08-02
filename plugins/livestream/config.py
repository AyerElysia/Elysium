"""Livestream 配置。

定义 AI 直播插件的所有配置节：
- 插件基础设置
- 平台配置（B站房间、认证）
- 管线配置（响应频率、批量窗口、上下文）
- 主动行为配置（空闲超时、话题、欢迎）
- TTS 配置（语速、音色、分句）
- 形象配置（Live2D 模型、表情映射）
- 服务器配置（路由、认证、心跳）
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class LivestreamConfig(BaseConfig):
    """AI 直播插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "AI 直播框架配置"

    # ------------------------------------------------------------------
    # 插件基础设置
    # ------------------------------------------------------------------

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=True,
            description="是否启用 AI 直播插件",
            label="启用插件",
            tag="plugin",
            order=0,
        )
        auto_start: bool = Field(
            default=False,
            description="系统启动时自动开始直播",
            label="自动开播",
            tag="plugin",
            order=1,
        )

    # ------------------------------------------------------------------
    # 平台配置
    # ------------------------------------------------------------------

    @config_section("platform", title="直播平台", tag="network", order=10)
    class PlatformSection(SectionBase):
        platform_type: str = Field(
            default="bilibili",
            description="直播平台类型（bilibili / douyin / twitch）",
            label="平台类型",
            tag="network",
            order=0,
        )
        room_id: str = Field(
            default="",
            description="直播间房间号",
            label="房间号",
            placeholder="12345678",
            tag="network",
            order=1,
        )
        sessdata: str = Field(
            default="",
            description="B站 SESSDATA（可选，用于发送弹幕和获取更多信息）",
            label="SESSDATA",
            tag="network",
            order=2,
        )
        buvid3: str = Field(
            default="",
            description="B站 buvid3（可选，配合 SESSDATA 使用）",
            label="buvid3",
            tag="network",
            order=3,
        )
        reconnect_interval: float = Field(
            default=5.0,
            description="断线重连间隔（秒）",
            label="重连间隔",
            tag="network",
            order=4,
        )

    # ------------------------------------------------------------------
    # 管线配置
    # ------------------------------------------------------------------

    @config_section("pipeline", title="互动管线", tag="ai", order=20)
    class PipelineSection(SectionBase):
        max_responses_per_minute: int = Field(
            default=6,
            description="每分钟最大回复次数（防止过度回复）",
            label="最大回复频率",
            tag="ai",
            order=0,
        )
        batch_window_seconds: float = Field(
            default=2.0,
            description="弹幕聚合窗口（秒），窗口内的弹幕打包为一次请求",
            label="聚合窗口",
            tag="ai",
            order=1,
        )
        max_context_turns: int = Field(
            default=20,
            description="LLM 上下文保留的最近互动轮数",
            label="上下文轮数",
            tag="ai",
            order=2,
        )
        max_queue_size: int = Field(
            default=50,
            description="优先级队列最大容量，溢出时丢弃低优先级",
            label="队列容量",
            tag="ai",
            order=3,
        )
        llm_timeout: float = Field(
            default=15.0,
            description="LLM 单次请求超时（秒）",
            label="LLM 超时",
            tag="ai",
            order=4,
        )
        llm_base_url: str = Field(
            default="http://127.0.0.1:3000/v1",
            description="OpenAI 兼容 LLM 服务地址；不得在 URL 中携带凭据",
            label="LLM 服务地址",
            tag="ai",
            order=5,
        )
        llm_model: str = Field(
            default="mimo-v2.5",
            description="直播表达使用的模型标识",
            label="LLM 模型",
            tag="ai",
            order=6,
        )
        llm_api_key_env: str = Field(
            default="LIVESTREAM_LLM_API_KEY",
            description="LLM API key 所在环境变量名；不得写入 key 本身",
            label="LLM 密钥环境变量",
            tag="ai",
            order=7,
        )
        min_danmaku_length: int = Field(
            default=2,
            description="弹幕最小有效长度（短于此值忽略）",
            label="最小弹幕长度",
            tag="ai",
            order=8,
        )
        dedup_window_seconds: float = Field(
            default=10.0,
            description="重复弹幕去重窗口（秒）",
            label="去重窗口",
            tag="ai",
            order=9,
        )

    # ------------------------------------------------------------------
    # 主动行为配置
    # ------------------------------------------------------------------

    @config_section("proactive", title="主动行为", tag="ai", order=30)
    class ProactiveSection(SectionBase):
        idle_timeout_seconds: float = Field(
            default=30.0,
            description="无互动后多少秒触发主动闲聊",
            label="空闲超时",
            tag="ai",
            order=0,
        )
        welcome_enabled: bool = Field(
            default=True,
            description="是否启用观众进场欢迎",
            label="进场欢迎",
            tag="ai",
            order=1,
        )
        welcome_batch_seconds: float = Field(
            default=10.0,
            description="进场欢迎聚合窗口（秒），批量欢迎",
            label="欢迎聚合窗口",
            tag="ai",
            order=2,
        )
        topic_switch_interval_seconds: float = Field(
            default=300.0,
            description="自动话题切换间隔（秒），0 表示禁用",
            label="话题切换间隔",
            tag="ai",
            order=3,
        )
        topics: list[str] = Field(
            default=[],
            description="主动话题列表（空闲时随机选取）",
            label="话题列表",
            tag="ai",
            order=4,
        )
        gift_thanks_enabled: bool = Field(
            default=True,
            description="是否启用礼物感谢",
            label="礼物感谢",
            tag="ai",
            order=5,
        )

    # ------------------------------------------------------------------
    # TTS 配置
    # ------------------------------------------------------------------

    @config_section("tts", title="语音合成", tag="audio", order=40)
    class TtsSection(SectionBase):
        tts_endpoint: str = Field(
            default="http://127.0.0.1:18082/send",
            description="本地 TTS 服务地址",
            label="TTS 地址",
            tag="audio",
            order=0,
        )
        speed: float = Field(
            default=1.0,
            description="语速倍率（0.5~2.0）",
            label="语速",
            tag="audio",
            order=1,
        )
        volume: float = Field(
            default=1.0,
            description="音量倍率（0.0~2.0）",
            label="音量",
            tag="audio",
            order=2,
        )
        sentence_delimiters: str = Field(
            default="。！？；\\n",
            description="分句分隔符（LLM 输出按此切分送 TTS）",
            label="分句符",
            tag="audio",
            order=3,
        )
        max_sentence_length: int = Field(
            default=80,
            description="单句最大字符数（超过则强制切分）",
            label="最大句长",
            tag="audio",
            order=4,
        )

    # ------------------------------------------------------------------
    # 形象配置
    # ------------------------------------------------------------------

    @config_section("avatar", title="虚拟形象", tag="visual", order=50)
    class AvatarSection(SectionBase):
        live2d_model_url: str = Field(
            default="",
            description="Live2D 模型文件 URL（留空使用默认）",
            label="模型地址",
            tag="visual",
            order=0,
        )
        expression_mapping: dict[str, str] = Field(
            default={
                "happy": "exp_01",
                "sad": "exp_02",
                "angry": "exp_03",
                "surprised": "exp_04",
                "neutral": "exp_00",
            },
            description="情绪 → Live2D 表情参数映射",
            label="表情映射",
            tag="visual",
            order=1,
        )
        idle_animation_enabled: bool = Field(
            default=True,
            description="是否启用空闲动画（眨眼、呼吸）",
            label="空闲动画",
            tag="visual",
            order=2,
        )
        background_color: str = Field(
            default="#1a1a2e",
            description="前端背景色（OBS 色键抠图用）",
            label="背景色",
            tag="visual",
            order=3,
        )
        transparent_background: bool = Field(
            default=False,
            description="透明背景模式（OBS 窗口捕获友好）",
            label="透明背景",
            tag="visual",
            order=4,
        )

    # ------------------------------------------------------------------
    # 服务器配置
    # ------------------------------------------------------------------

    @config_section("server", title="服务器设置", tag="network", order=60)
    class ServerSection(SectionBase):
        route_path: str = Field(
            default="/livestream",
            description="FastAPI 路由前缀路径",
            label="路由路径",
            tag="network",
            order=0,
        )
        auth_token: str = Field(
            default="",
            description="WebSocket 连接认证 token；留空则不验证",
            label="认证 Token",
            tag="network",
            order=1,
        )
        ws_heartbeat_interval: float = Field(
            default=15.0,
            description="WebSocket 心跳间隔（秒）",
            label="心跳间隔",
            tag="network",
            order=2,
        )

    # ------------------------------------------------------------------
    # 配置节实例声明（Pydantic 字段）
    # ------------------------------------------------------------------

    plugin: PluginSection = Field(default_factory=PluginSection)
    platform: PlatformSection = Field(default_factory=PlatformSection)
    pipeline: PipelineSection = Field(default_factory=PipelineSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    tts: TtsSection = Field(default_factory=TtsSection)
    avatar: AvatarSection = Field(default_factory=AvatarSection)
    server: ServerSection = Field(default_factory=ServerSection)
