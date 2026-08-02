"""Livestream technical bounds and migration-compatible configuration."""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import model_validator

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


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
            description="保留的兼容字段；必须为 false，直播只能由操作者手动开始",
            label="禁止自动开播",
            tag="plugin",
            order=1,
        )

        @model_validator(mode="after")
        def require_manual_start(self) -> LivestreamConfig.PluginSection:
            if self.auto_start:
                raise ValueError("livestream auto_start is forbidden; start manually")
            return self

    # ------------------------------------------------------------------
    # 平台配置
    # ------------------------------------------------------------------

    @config_section("platform", title="直播平台", tag="network", order=10)
    class PlatformSection(SectionBase):
        platform_type: str = Field(
            default="bilibili",
            description="直播平台类型；当前生产实现仅支持 bilibili",
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
            description="废弃兼容字段；运行时不会读取明文凭据，请使用环境变量",
            label="旧 SESSDATA（不使用）",
            tag="network",
            order=2,
        )
        buvid3: str = Field(
            default="",
            description="废弃兼容字段；运行时不会读取明文凭据，请使用环境变量",
            label="旧 buvid3（不使用）",
            tag="network",
            order=3,
        )
        reconnect_interval: float = Field(
            default=2.0,
            gt=0,
            description="首次断线重连基准（秒）",
            label="重连间隔",
            tag="network",
            order=4,
        )
        max_reconnect_interval: float = Field(
            default=60.0,
            gt=0,
            description="指数退避的最大间隔（秒）",
            label="最大重连间隔",
            tag="network",
            order=5,
        )
        heartbeat_interval: float = Field(
            default=30.0,
            gt=0,
            description="B站长连接心跳间隔（秒）",
            label="平台心跳间隔",
            tag="network",
            order=6,
        )
        startup_timeout: float = Field(
            default=30.0,
            gt=0,
            description="首次完成平台鉴权的等待上限（秒）",
            label="平台启动超时",
            tag="network",
            order=7,
        )
        max_packet_bytes: int = Field(
            default=8388608,
            ge=65536,
            le=33554432,
            description="单个B站网络帧的最大字节数",
            label="平台帧上限",
            tag="network",
            order=8,
        )
        sessdata_env: str = Field(
            default="ELYSIUM_BILIBILI_SESSDATA",
            description="可选 SESSDATA 所在环境变量名；配置文件不保存凭据",
            label="SESSDATA 环境变量",
            tag="security",
            order=9,
        )
        buvid3_env: str = Field(
            default="ELYSIUM_BILIBILI_BUVID3",
            description="可选 buvid3 所在环境变量名",
            label="buvid3 环境变量",
            tag="security",
            order=10,
        )

        @model_validator(mode="after")
        def validate_supported_platform(self) -> LivestreamConfig.PlatformSection:
            if self.platform_type.casefold() != "bilibili":
                raise ValueError("current livestream implementation supports bilibili only")
            if self.max_reconnect_interval < self.reconnect_interval:
                raise ValueError(
                    "max_reconnect_interval must be >= reconnect_interval"
                )
            room_id = self.room_id.strip()
            if room_id and (not room_id.isdigit() or int(room_id) <= 0):
                raise ValueError("Bilibili room_id must be a positive integer")
            return self

    # ------------------------------------------------------------------
    # 管线配置
    # ------------------------------------------------------------------

    @config_section("pipeline", title="旧互动配置（不使用）", tag="migration", order=20)
    class PipelineSection(SectionBase):
        """Retain old files without restoring rule-based cognition."""

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

    @config_section("director", title="导演与统一意识", tag="ai", order=25)
    class DirectorSection(SectionBase):
        model_task: str = Field(
            default="actor",
            min_length=1,
            description="复用项目模型配置中的任务名，不单独配置模型或密钥",
            label="模型任务",
            tag="ai",
            order=0,
        )
        timeout_seconds: float = Field(
            default=30.0,
            gt=0,
            description="一次导演判断的总等待上限（秒）",
            label="导演超时",
            tag="ai",
            order=1,
        )
        batch_limit: int = Field(
            default=50,
            ge=1,
            le=1000,
            description="一次投射给统一意识的原始事件数量上限，仅用于资源边界",
            label="单批事件上限",
            tag="performance",
            order=2,
        )
        retry_max_seconds: float = Field(
            default=60.0,
            gt=0,
            description="导演循环连续失败时的最大退避间隔",
            label="失败退避上限",
            tag="performance",
            order=3,
        )

    # ------------------------------------------------------------------
    # 主动行为配置
    # ------------------------------------------------------------------

    @config_section("proactive", title="旧主动行为配置（不使用）", tag="migration", order=30)
    class ProactiveSection(SectionBase):
        """Retain old files; initiative belongs to the unified consciousness."""

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
            min_length=1,
            description="本地 TTS 服务地址",
            label="TTS 地址",
            tag="audio",
            order=0,
        )
        speed: float = Field(
            default=1.0,
            ge=0.5,
            le=2.0,
            description="语速倍率（0.5~2.0）",
            label="语速",
            tag="audio",
            order=1,
        )
        volume: float = Field(
            default=1.0,
            ge=0.0,
            le=2.0,
            description="音量倍率（0.0~2.0）",
            label="音量",
            tag="audio",
            order=2,
        )
        sentence_delimiters: str = Field(
            default="。！？；\\n",
            min_length=1,
            description="分句分隔符（LLM 输出按此切分送 TTS）",
            label="分句符",
            tag="audio",
            order=3,
        )
        max_sentence_length: int = Field(
            default=80,
            ge=1,
            le=4000,
            description="单句最大字符数（超过则强制切分）",
            label="最大句长",
            tag="audio",
            order=4,
        )
        timeout_seconds: float = Field(
            default=30.0,
            gt=0,
            description="单句语音合成超时（秒）",
            label="合成超时",
            tag="audio",
            order=5,
        )
        retry_count: int = Field(
            default=1,
            ge=0,
            le=5,
            description="可重试网络错误的额外重试次数",
            label="合成重试",
            tag="audio",
            order=6,
        )
        max_audio_bytes: int = Field(
            default=33554432,
            ge=65536,
            le=134217728,
            description="单句合成音频的最大字节数",
            label="音频大小上限",
            tag="audio",
            order=7,
        )
        playback_timeout_seconds: float = Field(
            default=120.0,
            gt=0,
            description="等待舞台实际播放回执的上限（秒）",
            label="播放回执超时",
            tag="audio",
            order=8,
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
            description="废弃兼容字段；控制面只接受短期单次票据",
            label="旧认证 Token（不使用）",
            tag="network",
            order=1,
        )
        ws_heartbeat_interval: float = Field(
            default=15.0,
            gt=0,
            description="WebSocket 心跳间隔（秒）",
            label="心跳间隔",
            tag="network",
            order=2,
        )
        stage_send_timeout_seconds: float = Field(
            default=5.0,
            gt=0,
            le=60,
            description="向 OBS/浏览器舞台写入控制帧或音频的等待上限",
            label="舞台发送超时",
            tag="performance",
            order=3,
        )
        ticket_secret_env: str = Field(
            default="ELYSIUM_LIVESTREAM_TICKET_SECRET",
            description="短期控制票据签名密钥所在环境变量名",
            label="票据密钥环境变量",
            tag="security",
            order=4,
        )
        ticket_ttl_seconds: int = Field(
            default=30,
            ge=5,
            le=300,
            description="单次使用控制票据有效期（秒）",
            label="票据有效期",
            tag="security",
            order=5,
        )
        allowed_origins: list[str] = Field(
            default_factory=list,
            description="额外允许的浏览器来源；同源访问始终允许",
            label="浏览器来源白名单",
            tag="security",
            order=6,
        )
        max_stage_clients: int = Field(
            default=4,
            ge=1,
            le=32,
            description="同时连接的舞台/观察客户端上限",
            label="舞台连接上限",
            tag="performance",
            order=7,
        )
        operator_text_max_chars: int = Field(
            default=1000,
            ge=1,
            le=10000,
            description="操作者手动发言的最大字符数",
            label="手动发言上限",
            tag="security",
            order=8,
        )
        shutdown_timeout_seconds: float = Field(
            default=10.0,
            gt=0,
            le=120,
            description="每个直播资源关闭步骤的等待上限",
            label="单步关闭超时",
            tag="performance",
            order=9,
        )
        presence_lease_seconds: int = Field(
            default=300,
            ge=30,
            le=3600,
            description="直播意识实例的技术存活租约；运行时会定期续租",
            label="意识实例租约",
            tag="performance",
            order=10,
        )

        @model_validator(mode="after")
        def validate_control_surface(self) -> LivestreamConfig.ServerSection:
            if not self.route_path.startswith("/") or self.route_path == "/":
                raise ValueError("livestream route_path must be a non-root absolute path")
            normalized_origins: list[str] = []
            for raw in self.allowed_origins:
                try:
                    parsed = urlsplit(str(raw))
                    port = parsed.port
                except ValueError as exc:
                    raise ValueError(f"invalid livestream allowed origin: {raw}") from exc
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path not in {"", "/"}
                    or parsed.query
                    or parsed.fragment
                    or (port is not None and not 1 <= port <= 65535)
                ):
                    raise ValueError(f"invalid livestream allowed origin: {raw}")
                normalized_origins.append(str(raw).rstrip("/"))
            self.allowed_origins = normalized_origins
            return self

    @config_section("storage", title="直播记录", tag="storage", order=70)
    class StorageSection(SectionBase):
        ledger_path: str = Field(
            default="data/livestream/ledger.sqlite3",
            min_length=1,
            description="不可变直播事件账本路径",
            label="账本路径",
            tag="storage",
            order=0,
        )
        audio_artifact_path: str = Field(
            default="data/livestream/audio",
            min_length=1,
            description="内容寻址的合成音频缓存目录",
            label="音频记录目录",
            tag="storage",
            order=1,
        )

    # ------------------------------------------------------------------
    # 配置节实例声明（Pydantic 字段）
    # ------------------------------------------------------------------

    plugin: PluginSection = Field(default_factory=PluginSection)
    platform: PlatformSection = Field(default_factory=PlatformSection)
    pipeline: PipelineSection = Field(default_factory=PipelineSection)
    director: DirectorSection = Field(default_factory=DirectorSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    tts: TtsSection = Field(default_factory=TtsSection)
    avatar: AvatarSection = Field(default_factory=AvatarSection)
    server: ServerSection = Field(default_factory=ServerSection)
    storage: StorageSection = Field(default_factory=StorageSection)
