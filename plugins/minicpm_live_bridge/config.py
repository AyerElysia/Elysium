"""MiniCPM Live Bridge 配置。"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class MiniCPMLiveBridgeConfig(BaseConfig):
    """MiniCPM-o live 外部服务器桥接配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "MiniCPM-o live 外部服务器桥接配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=True,
            description="是否启用 MiniCPM Live Bridge",
            label="启用插件",
            tag="plugin",
            order=0,
        )

    @config_section("server", title="外部 Live 服务器", tag="network", order=10)
    class ServerSection(SectionBase):
        base_url: str = Field(
            default="",
            description="外部 live 服务器基础地址，例如 http://127.0.0.1:7860",
            label="服务器地址",
            placeholder="http://127.0.0.1:7860",
            tag="network",
            order=0,
        )
        health_url: str = Field(
            default="",
            description="健康检查地址；留空时使用 base_url",
            label="健康检查地址",
            placeholder="/health",
            tag="network",
            order=1,
        )
        session_url: str = Field(
            default="",
            description="可选：创建远端 live session 的 HTTP 地址",
            label="会话创建地址",
            placeholder="/api/sessions",
            tag="network",
            order=2,
        )
        websocket_url: str = Field(
            default="",
            description="可选：浏览器直连外部服务器的 WebSocket ingest 地址，支持 {session_id} 占位符",
            label="WebSocket 地址",
            placeholder="ws://127.0.0.1:7860/live?session_id={session_id}",
            tag="network",
            order=3,
        )
        frontend_url: str = Field(
            default="",
            description="可选：外部服务器自带 WebRTC 前端地址；配置后页面会提供直接打开入口",
            label="外部前端地址",
            placeholder="http://127.0.0.1:7860",
            tag="network",
            order=4,
        )
        livekit_url: str = Field(
            default="",
            description="可选：外部 LiveKit/WebRTC 服务地址，预留给后续官方 WebRTC 适配",
            label="LiveKit 地址",
            placeholder="ws://127.0.0.1:7880",
            tag="network",
            order=5,
        )
        token_url: str = Field(
            default="",
            description="可选：获取 WebRTC/LiveKit token 的地址，预留给后续官方 WebRTC 适配",
            label="Token 地址",
            placeholder="/api/token",
            tag="security",
            order=6,
        )
        auth_header: str = Field(
            default="Authorization",
            description="Neo 服务端访问外部服务器时使用的认证 Header 名称",
            label="认证 Header",
            tag="security",
            order=7,
        )
        auth_token: str = Field(
            default="",
            description="Neo 服务端访问外部服务器时使用的认证 token；不会下发给浏览器",
            label="服务器 Token",
            input_type="password",
            tag="security",
            order=8,
        )
        request_timeout_seconds: float = Field(
            default=3.0,
            ge=0.2,
            le=30.0,
            description="健康检查和会话创建请求超时时间",
            label="请求超时",
            tag="performance",
            order=9,
        )

    @config_section("capture", title="浏览器采集", tag="general", order=20)
    class CaptureSection(SectionBase):
        screen_fps: float = Field(
            default=5.0,
            ge=0.5,
            le=30.0,
            description="屏幕帧发送频率",
            label="屏幕 FPS",
            tag="performance",
            order=0,
        )
        screen_max_width: int = Field(
            default=1280,
            ge=320,
            le=3840,
            description="发送给外部服务器的屏幕帧最大宽度",
            label="屏幕宽度上限",
            tag="performance",
            order=1,
        )
        jpeg_quality: float = Field(
            default=0.72,
            ge=0.1,
            le=1.0,
            description="屏幕帧 JPEG 压缩质量",
            label="JPEG 质量",
            tag="performance",
            order=2,
        )
        audio_mime_type: str = Field(
            default="audio/webm;codecs=opus",
            description="MediaRecorder 音频编码格式",
            label="音频编码",
            tag="general",
            order=3,
        )
        audio_chunk_ms: int = Field(
            default=250,
            ge=100,
            le=2000,
            description="麦克风音频分片间隔",
            label="音频分片",
            tag="performance",
            order=4,
        )

    @config_section("session", title="统一事件流会话", tag="ai", order=30)
    class SessionSection(SectionBase):
        stream_id: str = Field(
            default="live_voice_main",
            description="live 语音通话写入 Neo 统一事件流时使用的固定 stream_id。它是来源/回复目标，不是独立意识 session。",
            label="Live Stream ID",
            tag="ai",
            order=0,
        )
        stream_name: str = Field(
            default="Live 语音通话",
            description="统一事件流中显示的 live 流名称",
            label="Live 流名称",
            tag="ai",
            order=1,
        )
        user_id: str = Field(
            default="live_user",
            description="live 用户在 Neo 消息模型中的 sender_id",
            label="Live 用户 ID",
            tag="user",
            order=2,
        )
        user_name: str = Field(
            default="Live 用户",
            description="live 用户在 Neo 消息模型中的显示名",
            label="Live 用户名",
            tag="user",
            order=3,
        )
        assistant_id: str = Field(
            default="live_assistant",
            description="live 模型输出写入统一事件流时使用的 sender_id",
            label="Live 助手 ID",
            tag="user",
            order=4,
        )
        assistant_name: str = Field(
            default="Live 模型",
            description="live 模型输出写入统一事件流时使用的显示名",
            label="Live 助手名",
            tag="user",
            order=5,
        )
        model_task_name: str = Field(
            default="live",
            description="live 外部服务器或后续 Neo realtime adapter 使用的模型任务名，对应 config/model.toml 中的 [model_tasks.live]。",
            label="Live 模型任务",
            tag="ai",
            order=6,
        )
        enable_local_api_turn: bool = Field(
            default=True,
            description="未配置外部 websocket_url 时，是否允许页面直接调用 Neo 的 /api/turn 单轮模型接口。",
            label="启用单轮 API 模式",
            tag="ai",
            order=7,
        )
        dispatch_user_transcript_to_chatter: bool = Field(
            default=False,
            description=(
                "是否把 live 用户语音转写发布为 ON_MESSAGE_RECEIVED 并唤醒核心 Chatter。"
                "默认关闭：live 模型自己负责全双工对话，Neo 只写统一事件流。"
            ),
            label="唤醒核心 Chatter",
            tag="ai",
            order=8,
        )
        tts_style: str = Field(
            default="default",
            description=(
                "本地单轮 API 模式回复后使用的 GPT-SoVITS 风格名称（对应 tts_voice_plugin 中配置的 style_name）。"
                "留空则跳过 GPT-SoVITS，前端回退到浏览器 TTS。"
            ),
            label="TTS 风格",
            tag="ai",
            order=9,
        )

    @config_section("vad", title="实时语音检测 (VAD)", tag="ai", order=35)
    class VadSection(SectionBase):
        threshold: float = Field(
            default=0.012,
            ge=0.001,
            le=0.5,
            description="判断为语音的麦克风 RMS 能量阈值（0.001–0.5）。越小越灵敏，建议 0.008–0.02。",
            label="语音阈值",
            tag="ai",
            order=0,
        )
        silence_ms: int = Field(
            default=800,
            ge=200,
            le=5000,
            description="停止说话后，静音持续多少毫秒触发发送。",
            label="静音截断 (ms)",
            tag="ai",
            order=1,
        )
        min_speech_ms: int = Field(
            default=300,
            ge=100,
            le=3000,
            description="触发发送所需的最短语音时长（毫秒）；低于此时长的片段将被丢弃。",
            label="最短语音 (ms)",
            tag="ai",
            order=2,
        )
        max_ms: int = Field(
            default=15000,
            ge=2000,
            le=60000,
            description="单次语音录制的最大时长（毫秒）；超出后强制发送。",
            label="最长录制 (ms)",
            tag="ai",
            order=3,
        )
        pre_speech_ms: int = Field(
            default=200,
            ge=0,
            le=1000,
            description="发送语音时在起始前额外保留多少毫秒的预录缓冲，捕捉音头。",
            label="预录缓冲 (ms)",
            tag="ai",
            order=4,
        )
        sensitivity: float = Field(
            default=3.0,
            ge=1.0,
            le=20.0,
            description="噪底倍数：语音触发阈值 = 环境噪底 × sensitivity。越大越不灵敏，越小越容易触发。建议 2.5–5.0。",
            label="灵敏度倍数",
            tag="ai",
            order=5,
        )
        speech_ratio: float = Field(
            default=0.35,
            ge=0.1,
            le=0.8,
            description="触发人声检测所需的语音频段（200–3500 Hz）能量占总能量的最低比例。提高此值可更严格地排除非人声（如音乐、敲击声）；建议 0.30–0.50。",
            label="语音频段占比",
            tag="ai",
            order=6,
        )

    @config_section("unified_event_stream", title="实时统一事件流", tag="network", order=40)
    class UnifiedEventStreamSection(SectionBase):
        sync_core_events_to_live: bool = Field(
            default=True,
            description="是否把 QQ/其他流的核心消息事件实时推给 live 连接",
            label="同步核心事件到 Live",
            tag="network",
            order=0,
        )
        ignore_live_echo_to_live: bool = Field(
            default=True,
            description="向 live 推送统一事件流时，是否跳过当前 live stream 自己刚写入的事件，避免回声",
            label="过滤 Live 回声",
            tag="network",
            order=1,
        )
        max_backlog_events: int = Field(
            default=800,
            ge=50,
            le=10000,
            description="MiniCPM Live Bridge 内存中保留的实时统一事件数量",
            label="事件缓存上限",
            tag="performance",
            order=2,
        )
        record_screen_summary_to_life: bool = Field(
            default=True,
            description="是否把外部 live 服务器返回的屏幕摘要写入 life_engine 统一事件流",
            label="记录屏幕摘要",
            tag="ai",
            order=3,
        )

    @config_section("context", title="Live 上下文", tag="ai", order=50)
    class ContextSection(SectionBase):
        include_life_runtime_context: bool = Field(
            default=True,
            description="本地单轮 API 和外部 live session 是否读取 life_engine 的统一意识运行态上下文。",
            label="接入统一意识上下文",
            tag="ai",
            order=0,
        )
        include_unified_events: bool = Field(
            default=True,
            description="本地单轮 API 和外部 live session 是否携带最近实时统一事件流。",
            label="接入实时事件流",
            tag="network",
            order=1,
        )
        include_live_session_events: bool = Field(
            default=True,
            description="是否把当前 live session 的最近用户/模型事件作为短期上下文传给模型。",
            label="接入 Live 短期记录",
            tag="ai",
            order=2,
        )
        life_event_limit: int = Field(
            default=80,
            ge=1,
            le=160,
            description="从 life_engine 运行态读取的事件窗口上限。",
            label="Life 事件窗口",
            tag="performance",
            order=3,
        )
        unified_event_limit: int = Field(
            default=80,
            ge=1,
            le=500,
            description="传给 live 模型的最近实时统一事件数量上限。",
            label="实时事件窗口",
            tag="performance",
            order=4,
        )
        live_session_event_limit: int = Field(
            default=24,
            ge=1,
            le=100,
            description="传给 live 模型的当前 live session 最近事件数量上限。",
            label="Live 事件窗口",
            tag="performance",
            order=5,
        )
        mark_life_context_seen: bool = Field(
            default=False,
            description=(
                "是否让 live 读取后推进 life_chatter 的全局上下文 cursor。"
                "默认关闭，避免 live 消耗 QQ 主链路尚未读取的新增事件。"
            ),
            label="推进主意识 Cursor",
            tag="advanced",
            order=6,
        )

    @config_section("debug", title="Live 调试日志", tag="debug", order=60)
    class DebugSection(SectionBase):
        terminal_log_enabled: bool = Field(
            default=True,
            description="是否把 live bridge 的关键事件打印到终端日志，方便调试语音/live 链路。",
            label="终端日志",
            tag="debug",
            order=0,
        )
        log_core_events: bool = Field(
            default=True,
            description="是否打印 QQ/其他核心消息同步到 live 统一事件流的摘要。",
            label="核心事件摘要",
            tag="debug",
            order=1,
        )
        log_prompt_preview: bool = Field(
            default=True,
            description="是否打印 life_chatter 同源 prompt 的长度与预览摘要。",
            label="Prompt 摘要",
            tag="debug",
            order=2,
        )
        log_client_events: bool = Field(
            default=True,
            description="是否打印浏览器 live 页面回传的状态、错误、语音和 WebSocket 调试日志。",
            label="前端事件摘要",
            tag="debug",
            order=3,
        )
        stderr_mirror_enabled: bool = Field(
            default=True,
            description="是否额外把 live 调试日志直接写入 stderr，绕过内核 logger 等级/事件链路，方便终端排查。",
            label="强制终端镜像",
            tag="debug",
            order=4,
        )
        preview_chars: int = Field(
            default=360,
            ge=80,
            le=4000,
            description="终端日志中文本预览的最大字符数。",
            label="预览字符数",
            tag="debug",
            order=5,
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    server: ServerSection = Field(default_factory=ServerSection)
    capture: CaptureSection = Field(default_factory=CaptureSection)
    session: SessionSection = Field(default_factory=SessionSection)
    vad: VadSection = Field(default_factory=VadSection)
    unified_event_stream: UnifiedEventStreamSection = Field(default_factory=UnifiedEventStreamSection)
    context: ContextSection = Field(default_factory=ContextSection)
    debug: DebugSection = Field(default_factory=DebugSection)
