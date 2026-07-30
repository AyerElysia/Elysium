"""Voice Live 配置。

定义全双工语音通话插件的所有配置节，包括：
- 插件基础设置
- 全双工 Provider 配置（OpenAI Realtime / Moshi）
- 降级管线配置（MiMo-V2.5 + IndexTTS2）
- VAD 语音活动检测参数
- 会话与意识实例配置
- 音频格式配置
"""

from __future__ import annotations

from typing import ClassVar, Literal

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class VoiceLiveConfig(BaseConfig):
    """全双工语音通话插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "全双工实时语音通话配置"

    # ------------------------------------------------------------------
    # 插件基础设置
    # ------------------------------------------------------------------

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=True,
            description="是否启用 Voice Live 插件",
            label="启用插件",
            tag="plugin",
            order=0,
        )

    # ------------------------------------------------------------------
    # 服务器设置
    # ------------------------------------------------------------------

    @config_section("server", title="服务器设置", tag="network", order=10)
    class ServerSection(SectionBase):
        route_path: str = Field(
            default="/voice-live",
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
        max_concurrent_sessions: int = Field(
            default=4,
            description="最大并发通话会话数",
            label="最大并发数",
            tag="network",
            order=2,
        )

    # ------------------------------------------------------------------
    # 全双工 Provider 配置
    # ------------------------------------------------------------------

    @config_section("full_duplex", title="全双工 Provider", tag="ai", order=20)
    class FullDuplexSection(SectionBase):
        provider_type: Literal["openai_realtime", "moshi", "disabled"] = Field(
            default="disabled",
            description="全双工 Provider 类型；disabled 时自动使用降级管线",
            label="Provider 类型",
            tag="ai",
            order=0,
        )
        upstream_url: str = Field(
            default="",
            description="上游全双工模型 WebSocket 地址，例如 wss://api.openai.com/v1/realtime 或 ws://127.0.0.1:8998/api/chat",
            label="上游地址",
            placeholder="wss://api.openai.com/v1/realtime",
            tag="network",
            order=1,
        )
        api_key: str = Field(
            default="",
            description="上游模型 API Key（OpenAI Realtime 需要；Moshi 本地可留空）",
            label="API Key",
            tag="network",
            order=2,
        )
        model_name: str = Field(
            default="gpt-4o-realtime-preview",
            description="全双工模型标识符",
            label="模型名称",
            tag="ai",
            order=3,
        )
        voice: str = Field(
            default="alloy",
            description="OpenAI Realtime 语音名称（alloy/echo/shimmer 等）",
            label="语音",
            tag="ai",
            order=4,
        )
        instructions: str = Field(
            default="",
            description="注入全双工模型的额外系统指令；留空则从意识上下文自动生成",
            label="系统指令",
            tag="ai",
            order=5,
        )
        connect_timeout: float = Field(
            default=10.0,
            description="连接上游模型的超时时间（秒）",
            label="连接超时",
            tag="network",
            order=6,
        )

    # ------------------------------------------------------------------
    # 降级管线配置
    # ------------------------------------------------------------------

    @config_section("degraded", title="降级管线", tag="ai", order=30)
    class DegradedSection(SectionBase):
        enabled: bool = Field(
            default=True,
            description="是否启用降级管线（全双工不可用时的回退方案）",
            label="启用降级",
            tag="ai",
            order=0,
        )
        model_task_name: str = Field(
            default="live",
            description="降级管线使用的模型任务名（对应 model.toml 中的 [model_tasks.live]）",
            label="模型任务",
            tag="ai",
            order=1,
        )
        tts_style: str = Field(
            default="default",
            description="TTS 语音风格（对应 tts_voice_plugin 中的 style_name）",
            label="TTS 风格",
            tag="ai",
            order=2,
        )
        sentence_min_chars: int = Field(
            default=8,
            description="凑够多少字符强制切句（防止长句缺标点不发声）",
            label="最小切句字符",
            tag="ai",
            order=3,
        )
        max_context_turns: int = Field(
            default=20,
            description="降级管线保留的最近对话轮数",
            label="上下文轮数",
            tag="ai",
            order=4,
        )
        llm_timeout: float = Field(
            default=60.0,
            description="LLM 请求超时时间（秒）",
            label="LLM 超时",
            tag="network",
            order=5,
        )

    # ------------------------------------------------------------------
    # VAD 语音活动检测
    # ------------------------------------------------------------------

    @config_section("vad", title="语音活动检测", tag="audio", order=40)
    class VadSection(SectionBase):
        threshold: float = Field(
            default=0.012,
            description="判断为语音的 RMS 能量阈值（0.001–0.5）",
            label="能量阈值",
            tag="audio",
            order=0,
        )
        silence_ms: int = Field(
            default=800,
            description="停止说话后静音持续多少毫秒触发发送",
            label="静音触发(ms)",
            tag="audio",
            order=1,
        )
        min_speech_ms: int = Field(
            default=300,
            description="触发发送所需的最短语音时长（毫秒）",
            label="最短语音(ms)",
            tag="audio",
            order=2,
        )
        max_ms: int = Field(
            default=15000,
            description="单次语音录制的最大时长（毫秒）",
            label="最大时长(ms)",
            tag="audio",
            order=3,
        )
        pre_speech_ms: int = Field(
            default=200,
            description="语音起始前额外保留的预录缓冲（毫秒）",
            label="预录缓冲(ms)",
            tag="audio",
            order=4,
        )

    # ------------------------------------------------------------------
    # 会话与意识实例
    # ------------------------------------------------------------------

    @config_section("session", title="会话设置", tag="session", order=50)
    class SessionSection(SectionBase):
        instance_id: str = Field(
            default="voice_live_001",
            description="意识实例 ID",
            label="实例 ID",
            tag="session",
            order=0,
        )
        stream_id: str = Field(
            default="voice_live_main",
            description="语音通话写入统一事件流时使用的 stream_id",
            label="Stream ID",
            tag="session",
            order=1,
        )
        stream_name: str = Field(
            default="语音通话",
            description="统一事件流中显示的流名称",
            label="流名称",
            tag="session",
            order=2,
        )
        user_id: str = Field(
            default="voice_user",
            description="语音用户在消息模型中的 sender_id",
            label="用户 ID",
            tag="session",
            order=3,
        )
        user_name: str = Field(
            default="语音用户",
            description="语音用户显示名",
            label="用户名",
            tag="session",
            order=4,
        )
        include_life_runtime_context: bool = Field(
            default=True,
            description="是否读取 life_engine 的运行态上下文（人格、记忆、WorldState）",
            label="包含运行态上下文",
            tag="session",
            order=5,
        )
        include_unified_events: bool = Field(
            default=True,
            description="是否携带最近统一事件流",
            label="包含统一事件",
            tag="session",
            order=6,
        )
        record_to_life: bool = Field(
            default=True,
            description="是否将通话事件写入 life_engine 统一事件流",
            label="记录到生命引擎",
            tag="session",
            order=7,
        )

    # ------------------------------------------------------------------
    # 音频格式
    # ------------------------------------------------------------------

    @config_section("audio", title="音频设置", tag="audio", order=60)
    class AudioSection(SectionBase):
        input_sample_rate: int = Field(
            default=16000,
            description="客户端输入音频采样率（Hz）",
            label="输入采样率",
            tag="audio",
            order=0,
        )
        output_sample_rate: int = Field(
            default=24000,
            description="服务端输出音频采样率（Hz）",
            label="输出采样率",
            tag="audio",
            order=1,
        )
        chunk_ms: int = Field(
            default=100,
            description="音频分片间隔（毫秒）",
            label="分片间隔(ms)",
            tag="audio",
            order=2,
        )
        format: Literal["pcm16", "opus"] = Field(
            default="pcm16",
            description="WebSocket 传输的音频格式",
            label="音频格式",
            tag="audio",
            order=3,
        )

    # ------------------------------------------------------------------
    # 配置节实例声明（Pydantic 字段）
    # ------------------------------------------------------------------

    plugin: PluginSection = Field(default_factory=PluginSection)
    server: ServerSection = Field(default_factory=ServerSection)
    full_duplex: FullDuplexSection = Field(default_factory=FullDuplexSection)
    degraded: DegradedSection = Field(default_factory=DegradedSection)
    vad: VadSection = Field(default_factory=VadSection)
    session: SessionSection = Field(default_factory=SessionSection)
    audio: AudioSection = Field(default_factory=AudioSection)
