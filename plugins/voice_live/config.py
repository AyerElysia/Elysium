"""Commercial realtime voice configuration.

The provider is always explicit.  A failed provider never changes the model or
conversation architecture behind the user's back.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from src.core.components.base.config import (
    BaseConfig,
    Field,
    SectionBase,
    config_section,
)


class VoiceLiveConfig(BaseConfig):
    """Configuration for the independent ``voice_live`` consciousness."""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "商业级全双工实时语音通话配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=True, description="是否启用 Voice Live", label="启用插件"
        )

    @config_section("server", title="接入与会话安全", tag="network", order=10)
    class ServerSection(SectionBase):
        route_path: str = Field(default="/voice-live", description="HTTP 路由前缀")
        allowed_origins: list[str] = Field(
            default_factory=lambda: [
                "http://127.0.0.1",
                "http://localhost",
                "https://127.0.0.1",
                "https://localhost",
            ],
            description="允许申请短期 WebSocket ticket 的浏览器 Origin 前缀",
        )
        ticket_secret_env: str = Field(
            default="VOICE_LIVE_TICKET_SECRET",
            description="保存 ticket 签名密钥的环境变量名；本机未设置时使用进程级随机密钥",
        )
        ticket_ttl_seconds: int = Field(
            default=30, ge=5, description="一次性连接 ticket 有效期"
        )
        max_concurrent_sessions: int = Field(
            default=1, ge=1, description="最大并发通话数"
        )
        max_session_minutes: int = Field(
            default=120, ge=1, description="单次通话硬上限"
        )
        idle_timeout_seconds: int = Field(
            default=300, ge=30, description="无音频与控制消息的空闲超时"
        )

    @config_section("full_duplex", title="实时 Omni Provider", tag="ai", order=20)
    class FullDuplexSection(SectionBase):
        provider_type: Literal[
            "minicpm_omni",
            "qwen_realtime",
            "openai_realtime",
            "disabled",
        ] = Field(
            default="minicpm_omni",
            description="显式选择实时供应商；连接失败时不会隐式更换模型",
        )
        upstream_url: str = Field(
            default="ws://127.0.0.1:9060/backend",
            description="实时模型 WebSocket 地址",
        )
        api_key_env: str = Field(
            default="VOICE_LIVE_API_KEY",
            description="云端 API Key 所在环境变量名；密钥本身不得写入配置",
        )
        api_key_file: str = Field(
            default="",
            description="云端 API Key 的 owner-only 本地文件；环境变量优先",
        )
        model_name: str = Field(default="MiniCPM-o-4_5", description="实时模型标识")
        voice: str = Field(
            default="", description="云端 voice 标识；空值使用供应商默认语音"
        )
        mode: Literal["full_duplex"] = Field(
            default="full_duplex",
            description="MiniCPM-o 运行模式",
        )
        reference_audio_path: str = Field(
            default="",
            description="本地 Omni 输入参考音频（16k mono float32/WAV）；空值不注入",
        )
        tts_reference_audio_path: str = Field(
            default="",
            description="本地 Omni 音色参考音频；空值使用运行时默认",
        )
        upstream_input_chunk_ms: int = Field(
            default=1000,
            ge=20,
            description="聚合后发往上游的音频时长；MiniCPM-o 官方全双工路径建议 1000ms",
        )
        connect_timeout_seconds: float = Field(
            default=20.0, gt=0, description="上游连接超时"
        )
        event_timeout_seconds: float = Field(
            default=45.0, gt=0, description="会话初始化事件超时"
        )
        instructions: str = Field(
            default="",
            description="实时语音交互覆盖层；不是身份权威，过长时只投影到有界上下文",
        )
        qwen_max_history_turns: int = Field(
            default=12,
            ge=1,
            le=50,
            description="Qwen-Audio 上游保留的最近问答轮数；完整历史仍写入 episode 与 Life Event",
        )

    @config_section("voice_conversion", title="爱莉实时音色", tag="audio", order=25)
    class VoiceConversionSection(SectionBase):
        enabled: bool = Field(
            default=False,
            description="把 Realtime Provider 的语音流送入外部 SVC 服务；启用后服务不可用会显式终止会话",
        )
        service_url: str = Field(
            default="http://127.0.0.1:17861",
            description="独立实时变声服务地址；WSL2 NAT 下需配置为当前 Windows 主机地址",
        )
        token_env: str = Field(
            default="SEEDVC_STREAM_TOKEN",
            description="本地变声服务 bearer token 所在环境变量名；不得写入 token 本身",
        )
        token_file: str = Field(
            default="",
            description="变声服务 bearer token 的 owner-only 本地文件；环境变量优先",
        )
        profile_id: str = Field(
            default="elysia",
            description="服务端预注册的目标音色 profile ID",
        )
        connect_timeout_seconds: float = Field(
            default=10.0,
            gt=0,
            description="变声服务连接与会话创建超时",
        )
        activation_timeout_seconds: float = Field(
            default=120.0,
            gt=0,
            description="SeedVC 首次 Voice 会话加载模型与预热的超时时间",
        )
        request_timeout_seconds: float = Field(
            default=10.0,
            gt=0,
            description="单个流式音频块转换超时",
        )
        queue_max_chunks: int = Field(
            default=64,
            ge=4,
            description="Provider 与 SVC 之间的有界队列；溢出时显式失败而非继续堆积延迟",
        )

    @config_section("session", title="意识实例", tag="session", order=30)
    class SessionSection(SectionBase):
        instance_id_prefix: str = Field(
            default="voice_live", description="独立意识实例 ID 前缀"
        )
        stream_id_prefix: str = Field(
            default="voice_live", description="统一事件流 ID 前缀"
        )
        display_name: str = Field(default="实时通话意识", description="意识实例显示名")
        stream_name: str = Field(default="实时语音通话", description="统一事件流显示名")
        user_id: str = Field(default="voice_user", description="通话对方 sender_id")
        user_name: str = Field(default="用户", description="通话对方显示名")
        require_life_engine: bool = Field(
            default=True,
            description="要求真实注册到 LifeEngine；关闭仅用于隔离测试",
        )
        record_to_life: bool = Field(
            default=True, description="把最终转写写入统一生命事件流"
        )
        cross_scene_awareness: bool = Field(
            default=True, description="感知完整 WorldState"
        )

        subject_context_max_bytes: int = Field(
            default=24 * 1024,
            ge=8 * 1024,
            le=96 * 1024,
            description="从 SOUL/USER/MEMORY 统一权威派生的 Voice 专用主体投影预算",
        )
        episode_context_max_bytes: int = Field(
            default=12 * 1024,
            ge=2 * 1024,
            le=64 * 1024,
            description="重连时最近完整语音轮次的续接投影预算；完整 episode 不截断",
        )
        voice_instructions_max_bytes: int = Field(
            default=4 * 1024,
            ge=2 * 1024,
            le=32 * 1024,
            description="非权威实时语音交互覆盖层预算",
        )
        tool_result_context_max_bytes: int = Field(
            default=8 * 1024,
            ge=2 * 1024,
            le=64 * 1024,
            description="单次工具结果送入实时 Provider 的有界投影预算；原始工具来源不由该投影替代",
        )
        startup_context_max_bytes: int = Field(
            default=48 * 1024,
            ge=16 * 1024,
            le=192 * 1024,
            description="主体投影、episode 续接、语音覆盖层和固定契约的启动总预算",
        )

        perception_context_max_bytes: int = Field(
            default=8 * 1024,
            ge=4096,
            le=128 * 1024,
            description="实时 Provider 单次注入的低延迟世界感知胶囊字节上限；完整投影仍由 LifeEngine 保留",
        )

    @config_section("audio", title="音频传输", tag="audio", order=40)
    class AudioSection(SectionBase):
        input_sample_rate: int = Field(
            default=16000, ge=8000, description="上行 PCM16 采样率"
        )
        output_sample_rate: int = Field(
            default=24000, ge=8000, description="下行 PCM16 采样率"
        )
        browser_frame_ms: int = Field(default=20, ge=10, description="浏览器上行帧时长")
        channels: Literal[1] = Field(default=1, description="传输声道数")
        format: Literal["pcm16"] = Field(default="pcm16", description="浏览器传输格式")

    @config_section("observability", title="可观测性与隐私", tag="debug", order=50)
    class ObservabilitySection(SectionBase):
        trace_root: str = Field(
            default="runtime/consciousness",
            description="意识实例 episode、checkpoint 与 JSONL 轨迹目录",
        )
        persist_audio: bool = Field(
            default=False,
            description="是否分轨保存用户输入、模型原声与 SeedVC 输出；默认关闭以保护隐私",
        )
        metrics_interval_seconds: int = Field(
            default=5, ge=1, description="向浏览器推送指标的周期"
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    server: ServerSection = Field(default_factory=ServerSection)
    full_duplex: FullDuplexSection = Field(default_factory=FullDuplexSection)
    voice_conversion: VoiceConversionSection = Field(
        default_factory=VoiceConversionSection
    )
    session: SessionSection = Field(default_factory=SessionSection)
    audio: AudioSection = Field(default_factory=AudioSection)
    observability: ObservabilitySection = Field(default_factory=ObservabilitySection)
