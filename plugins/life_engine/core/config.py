"""life_engine 插件配置。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from pydantic import field_validator

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section

from ..constants import (
    HEARTBEAT_IDLE_CRITICAL_THRESHOLD,
    HEARTBEAT_IDLE_WARNING_THRESHOLD,
    EXTERNAL_MESSAGE_ACTIVE_WINDOW_MINUTES,
    TODO_URGENT_DAYS_THRESHOLD,
    RRF_K,
    SPREAD_DECAY,
    SPREAD_THRESHOLD,
    DECAY_LAMBDA,
    PRUNE_THRESHOLD,
    DREAM_LEARNING_RATE,
)


# 默认工作空间路径
_DEFAULT_WORKSPACE = str(Path(__file__).parent.parent.parent / "data" / "life_engine_workspace")


class LifeEngineConfig(BaseConfig):
    """life_engine 插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "生命中枢最小原型配置"

    @config_section("settings")
    class SettingsSection(SectionBase):
        """基础设置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 life_engine。设为 false 时不启动心跳。",
        )

        heartbeat_interval_seconds: int = Field(
            default=30,
            description="心跳间隔（秒）。",
        )

        sleep_time: str = Field(
            default="",
            description="睡觉时间，格式 HH:MM（24小时制）。与 wake_time 同时配置后生效。",
        )

        wake_time: str = Field(
            default="",
            description="苏醒时间，格式 HH:MM（24小时制）。与 sleep_time 同时配置后生效。",
        )

        log_heartbeat: bool = Field(
            default=True,
            description="是否在每次心跳时输出日志。",
        )

        context_history_max_events: int = Field(
            default=100,
            ge=1,
            description="滚动事件流最多保留的事件条数（包括心跳、消息、工具调用等）。",
        )

        workspace_path: str = Field(
            default=_DEFAULT_WORKSPACE,
            description="中枢文件系统操作的工作空间路径。中枢只能在此目录下进行文件操作。",
        )

        max_rounds_per_heartbeat: int = Field(
            default=3,
            ge=1,
            description="单次心跳内允许模型连续进行工具调用的最大轮数（防止死循环）。",
        )

        idle_pause_after_external_silence_minutes: int = Field(
            default=30,
            ge=0,
            description=(
                "外界入站消息静默达到多少分钟后暂停 life LLM 心跳。"
                "设为 0 表示不因外界静默暂停。手动心跳不受影响。"
            ),
        )

    @config_section("model")
    class ModelSection(SectionBase):
        """中枢模型任务设置。"""

        task_name: str = Field(
            default="life",
            description="中枢任务使用的模型任务名称，对应 config/model.toml 中的 [model_tasks.life]。",
        )

    @config_section("history_retrieval")
    class HistoryRetrievalSection(SectionBase):
        """聊天历史检索与回补配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用聊天历史检索工具。",
        )

        default_cross_stream: bool = Field(
            default=False,
            description="未显式指定 stream_id 时，是否默认跨 stream 检索。建议保持 false，让聊天态默认只查当前流。",
        )

        adapter_signature: str = Field(
            default="napcat_adapter:adapter:napcat_adapter",
            description="用于回补历史的适配器签名。",
        )

        group_history_actions: list[str] = Field(
            default_factory=lambda: ["get_group_msg_history"],
            description="群聊历史回补 action 候选列表（按顺序尝试）。",
        )

        private_history_actions: list[str] = Field(
            default_factory=lambda: [
                "get_friend_msg_history",
                "get_private_msg_history",
            ],
            description="私聊历史回补 action 候选列表（按顺序尝试）。",
        )

        adapter_timeout_seconds: int = Field(
            default=8,
            ge=1,
            le=60,
            description="适配器回补超时时间（秒）。",
        )

        max_candidate_streams: int = Field(
            default=12,
            ge=1,
            le=100,
            description="跨 stream 检索时最多扫描多少个候选流。",
        )

        max_scan_rows_per_stream: int = Field(
            default=240,
            ge=20,
            le=2000,
            description="每个 stream 最多扫描多少条历史消息。",
        )

        tool_default_limit: int = Field(
            default=20,
            ge=1,
            le=100,
            description="历史检索工具默认返回条数。",
        )

        tool_max_limit: int = Field(
            default=100,
            ge=10,
            le=500,
            description="历史检索工具允许返回的最大条数。",
        )

    @config_section("web")
    class WebSection(SectionBase):
        """网络搜索与网页提取能力配置（Tavily）。"""

        tavily_api_key: str = Field(
            default="",
            description="Tavily API Key。请在 config/plugins/life_engine/config.toml 的 [web] 中配置。",
        )

        tavily_api_keys: list[str] = Field(
            default_factory=list,
            description="多个 Tavily API Key。配置后 web_tools 会按轮询方式选择，用于负载均衡。",
        )

        tavily_base_url: str = Field(
            default="https://api.tavily.com",
            description="Tavily API 基础地址。",
        )

        tavily_base_urls: list[str] = Field(
            default_factory=list,
            description="多个 Tavily API 基础地址。配置后 web_tools 会按轮询方式选择，用于负载均衡。",
        )

        search_timeout_seconds: int = Field(
            default=30,
            ge=1,
            le=120,
            description="网络搜索超时（秒）。",
        )

        extract_timeout_seconds: int = Field(
            default=60,
            ge=1,
            le=180,
            description="网页提取超时（秒）。",
        )

        default_search_max_results: int = Field(
            default=5,
            ge=1,
            le=20,
            description="网络搜索默认返回条数。",
        )

        default_fetch_max_chars: int = Field(
            default=12000,
            ge=500,
            le=50000,
            description="网页提取默认最大返回字符数。",
        )

    @config_section("snn")
    class SNNSection(SectionBase):
        """SNN 皮层下状态层配置。"""

        enabled: bool = Field(
            default=False,
            description="是否启用 SNN 状态层。启用后 life_engine 将运行一个持续的 SNN 驱动核。",
        )

        shadow_only: bool = Field(
            default=True,
            description="影子模式：只记录 SNN 状态变化，不注入心跳 prompt。用于初期验证。",
        )

        tick_interval_seconds: float = Field(
            default=10.0,
            ge=1.0,
            description="SNN 独立 tick 间隔（秒）。SNN 以此频率独立更新衰减，不绑定 LLM 心跳。",
        )

        inject_to_heartbeat: bool = Field(
            default=False,
            description="是否将 SNN 驱动状态注入心跳 prompt。需要 shadow_only=false 才生效。",
        )

        feature_window_seconds: float = Field(
            default=600.0,
            ge=60.0,
            description="特征提取窗口大小（秒）。决定 SNN 从多长时间的事件中提取输入。",
        )

    @config_section("neuromod")
    class NeuromodSection(SectionBase):
        """神经调质层配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用神经调质层。调质层在 SNN 之上提供慢时间尺度的驱动调节。",
        )

        inject_to_heartbeat: bool = Field(
            default=True,
            description="是否将调质状态注入心跳 prompt。",
        )

        habit_tracking: bool = Field(
            default=True,
            description="是否启用习惯追踪。",
        )

    @config_section("dream")
    class DreamSection(SectionBase):
        """做梦系统配置。三阶段做梦周期：NREM 回放 → REM 联想 → 觉醒过渡。"""

        enabled: bool = Field(
            default=True,
            description="是否启用做梦系统。",
        )

        # NREM 参数
        nrem_replay_episodes: int = Field(
            default=3,
            ge=1, le=10,
            description="每次做梦 NREM 阶段回放的事件集数。",
        )

        nrem_events_per_episode: int = Field(
            default=20,
            ge=5, le=100,
            description="每集回放包含的事件数量。",
        )

        nrem_speed_multiplier: float = Field(
            default=5.0,
            ge=1.0, le=20.0,
            description="NREM 回放加速倍率（缩短 SNN tau）。",
        )

        nrem_homeostatic_rate: float = Field(
            default=0.02,
            ge=0.001, le=0.1,
            description="SHY 突触稳态缩减比例（每次做梦全局权重缩减百分比）。",
        )

        # REM 参数
        rem_walk_rounds: int = Field(
            default=2,
            ge=1, le=10,
            description="REM 阶段记忆图谱随机游走轮数。",
        )

        rem_seeds_per_round: int = Field(
            default=5,
            ge=1, le=20,
            description="每轮 REM 游走的随机种子数。",
        )

        rem_max_depth: int = Field(
            default=3,
            ge=1, le=5,
            description="REM 游走激活扩散最大深度。",
        )

        rem_decay_factor: float = Field(
            default=0.6,
            ge=0.1, le=0.95,
            description="REM 游走激活扩散衰减因子。",
        )

        rem_learning_rate: float = Field(
            default=0.05,
            ge=0.01, le=0.3,
            description="REM 阶段 Hebbian 学习率（低于清醒时 0.1）。",
        )

        rem_edge_prune_threshold: float = Field(
            default=0.08,
            ge=0.01, le=0.3,
            description="REM 阶段弱边修剪阈值（仅 ASSOCIATES 边）。",
        )

        # 调度参数
        dream_interval_minutes: int = Field(
            default=90,
            ge=10, le=480,
            description="两次做梦之间的最小间隔（分钟）。",
        )

        idle_trigger_heartbeats: int = Field(
            default=10,
            ge=3, le=50,
            description="白天连续空闲心跳数触发小憩做梦。",
        )

        nap_enabled: bool = Field(
            default=True,
            description="是否启用白天小憩做梦（空闲触发）。",
        )

    @config_section("thresholds")
    class ThresholdsSection(SectionBase):
        """阈值配置。"""

        external_active_minutes: int = Field(
            default=EXTERNAL_MESSAGE_ACTIVE_WINDOW_MINUTES,
            ge=1,
            description="外部消息活跃时间窗口（分钟）",
        )

        idle_warning_threshold: int = Field(
            default=HEARTBEAT_IDLE_WARNING_THRESHOLD,
            ge=1,
            description="心跳空闲警告阈值",
        )

        idle_critical_threshold: int = Field(
            default=HEARTBEAT_IDLE_CRITICAL_THRESHOLD,
            ge=1,
            description="心跳空闲严重警告阈值",
        )

        todo_urgent_days: int = Field(
            default=TODO_URGENT_DAYS_THRESHOLD,
            ge=1,
            description="TODO 紧急截止天数阈值",
        )

    @config_section("memory_algorithm")
    class MemoryAlgorithmSection(SectionBase):
        """记忆算法参数配置。"""

        rrf_k: int = Field(
            default=RRF_K,
            ge=1,
            description="RRF 融合参数",
        )

        spread_decay: float = Field(
            default=SPREAD_DECAY,
            ge=0.0,
            le=1.0,
            description="激活扩散衰减系数",
        )

        spread_threshold: float = Field(
            default=SPREAD_THRESHOLD,
            ge=0.0,
            le=1.0,
            description="激活扩散阈值",
        )

        decay_lambda: float = Field(
            default=DECAY_LAMBDA,
            ge=0.0,
            le=1.0,
            description="遗忘衰减系数",
        )

        prune_threshold: float = Field(
            default=PRUNE_THRESHOLD,
            ge=0.0,
            le=1.0,
            description="边剪枝阈值",
        )

        dream_learning_rate: float = Field(
            default=DREAM_LEARNING_RATE,
            ge=0.0,
            le=1.0,
            description="梦境学习率",
        )

    @config_section("chatter")
    class ChatterSection(SectionBase):
        """统一对话器配置。"""

        enabled: bool = Field(
            default=False,
            description="启用后 life_engine 直接处理对话，作为同一主体的对外运行模式。",
        )

        mode: str = Field(
            default="enhanced",
            description="执行模式: enhanced / classical",
        )

        max_rounds_per_chat: int = Field(
            default=5,
            ge=1,
            description="对话模式单轮最大工具调用轮数。",
        )

        initial_history_messages: int = Field(
            default=30,
            ge=0,
            description=(
                "life_chatter 首轮合并到 <chat_history> 的历史消息条数。"
                "设为 0 表示不注入历史消息。"
            ),
        )

        recent_history_tail_messages: int = Field(
            default=0,
            ge=0,
            description=(
                "兼容旧配置：若 initial_history_messages 未显式配置且此值 > 0，"
                "则回退使用该值作为首轮历史消息条数。"
            ),
        )

    @config_section("multimodal")
    class MultimodalSection(SectionBase):
        """life_chatter 原生多模态输入配置（MiMo-V2-Omni 等模型）。

        启用后会把 unread_msgs 中允许的媒体转为 LLM 原生 Content。
        当前默认只开启图片；视频/音频保留降级路径，避免模型拒收导致上下文链路中断。
        """

        enabled: bool = Field(
            default=False,
            description="启用 life_chatter 原生多模态输入。",
        )
        native_image: bool = Field(
            default=True,
            description="是否把 image 媒体作为原生 Image Content 注入。",
        )
        native_emoji: bool = Field(
            default=False,
            description="是否把 emoji / 表情包媒体作为原生 Image Content 注入。默认关闭，避免浪费多模态预算。",
        )
        native_video: bool = Field(
            default=False,
            description="是否把 video 媒体作为原生 Video Content 注入。",
        )
        native_audio: bool = Field(
            default=False,
            description="是否把 voice / record / audio 媒体作为原生 Audio Content 注入。",
        )
        max_images_per_payload: int = Field(
            default=4,
            ge=0,
            description="单次 USER payload 中最多注入的 image+emoji 数量。",
        )
        max_videos_per_payload: int = Field(
            default=1,
            ge=0,
            description="单次 USER payload 中最多注入的 video 数量。",
        )
        max_audios_per_payload: int = Field(
            default=2,
            ge=0,
            description="单次 USER payload 中最多注入的 voice/audio 数量。",
        )
        include_history_media: bool = Field(
            default=False,
            description=(
                "是否对 history（非 unread）消息也提取媒体。开启后，爱莉能在后续轮次"
                "重新看到自己刚发送/生成的图片。"
            ),
        )
        history_media_tail_messages: int = Field(
            default=20,
            ge=0,
            description="从最近多少条 history 消息里寻找可注入媒体。只影响 include_history_media=true 的情况。",
        )
        audio_max_seconds: int = Field(
            default=60,
            ge=1,
            description="单段语音/音频最大时长（秒）；超过则降级为 [语音消息] 文本占位。",
        )
        prune_old_media_after_send: bool = Field(
            default=True,
            description=(
                "回复成功后，把已发送的 USER payload 中的 Image/Audio/Video 替换为文本占位"
                "（如 [已发送语音:#mid]），避免后续轮次重复携带 base64 体积。"
            ),
        )
        unsupported_audio_placeholder: str = Field(
            default="[语音消息]",
            description="未知/不支持的音频格式（如 silk/amr）降级为该文本占位。",
        )

    @config_section("screen")
    class ScreenSection(SectionBase):
        """电脑屏幕观察工具配置。"""

        enabled: bool = Field(
            default=False,
            description="是否启用 nucleus_view_screen，让 life_chatter / life heartbeat 可按需截取并观察当前电脑屏幕。",
        )

        capture_method: str = Field(
            default="auto",
            description="截屏方式：auto / ffmpeg / grim / pil / powershell。"
            "auto 模式在 WSL 环境下自动优先使用 powershell（避免 x11grab 黑屏问题）；"
            "非 WSL 环境下优先使用 ffmpeg。"
            "powershell 仅在 WSL（Windows Subsystem for Linux）中有效，通过 .NET SetProcessDPIAware 截取完整物理分辨率桌面。",
        )

        display: str = Field(
            default="",
            description="X11 DISPLAY。留空时读取环境变量 DISPLAY，仍为空则回退到 :0。",
        )

        screen_width: int = Field(
            default=0,
            ge=0,
            description="截屏宽度。0 表示自动从 xdpyinfo 检测，检测失败时回退到 2560。",
        )

        screen_height: int = Field(
            default=0,
            ge=0,
            description="截屏高度。0 表示自动从 xdpyinfo 检测，检测失败时回退到 1440。",
        )

        max_width: int = Field(
            default=2560,
            ge=0,
            description="截图进入视觉模型前的最大宽度。0 表示不缩放。",
        )

        max_height: int = Field(
            default=1600,
            ge=0,
            description="截图进入视觉模型前的最大高度。0 表示不缩放。2K/高分屏默认完整保留。",
        )

        output_format: str = Field(
            default="png",
            description="截图图片格式：png / jpeg / webp。默认 png，适合看代码和文字。",
        )

        jpeg_quality: int = Field(
            default=92,
            ge=1,
            le=100,
            description="jpeg/webp 输出质量。",
        )

        capture_cursor: bool = Field(
            default=True,
            description="ffmpeg x11grab 截图时是否包含鼠标指针。",
        )

        capture_timeout_seconds: int = Field(
            default=20,
            ge=1,
            description="截屏命令超时时间。",
        )

        native_when_available: bool = Field(
            default=True,
            description="auto 模式下优先用 screen.native_task_name 指定的模型任务原生看图；与 life_chatter 原生媒体注入开关独立。",
        )

        native_task_name: str = Field(
            default="",
            description="原生看屏幕使用的模型任务名。留空时使用 [model].task_name。",
        )

        fallback_task_name: str = Field(
            default="vlm",
            description="原生不可用或失败时使用的 VLM 降级模型任务名。",
        )

        save_latest: bool = Field(
            default=False,
            description="是否把最近一次截图保存到 workspace。默认 false，避免无意持久化屏幕隐私。",
        )

        latest_path: str = Field(
            default="screenshots/latest_screen.png",
            description="save_latest=true 时的 workspace 相对保存路径。",
        )

        max_observation_chars: int = Field(
            default=2400,
            ge=200,
            description="工具返回给 LLM 的屏幕观察摘要最大字符数。",
        )

    @config_section("drives")
    class DrivesSection(SectionBase):
        """冲动引擎配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用冲动引擎。冲动引擎将神经调质状态转化为具体行为建议。",
        )

        inject_to_heartbeat: bool = Field(
            default=True,
            description="是否将冲动建议注入心跳 prompt。",
        )

        curiosity_threshold: float = Field(
            default=0.65,
            ge=0.3,
            le=0.9,
            description="好奇心冲动触发阈值。",
        )

        sociability_threshold: float = Field(
            default=0.6,
            ge=0.3,
            le=0.9,
            description="社交欲冲动触发阈值。",
        )

        silence_trigger_minutes: int = Field(
            default=30,
            ge=5,
            le=120,
            description="沉默多久后触发社交冲动（分钟）。",
        )

    @config_section("streams")
    class StreamsSection(SectionBase):
        """思考流系统配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用思考流系统。思考流给爱莉持久在意的兴趣线索，让她在心跳间有事可想。",
        )

        max_active_streams: int = Field(
            default=5,
            ge=1,
            le=10,
            description="同时活跃的思考流上限。超过后自动将好奇心最低的转入休眠。",
        )

        dormancy_threshold_hours: int = Field(
            default=24,
            ge=1,
            le=72,
            description="多久不推进后自动进入休眠（小时）。",
        )

        inject_to_heartbeat: bool = Field(
            default=True,
            description="是否将思考流状态注入心跳 prompt。",
        )

        sync_to_chatter: bool = Field(
            default=True,
            description="是否将思考流作为注意力脑区同步给 life_chatter。关闭后 chatter transient 中不再注入思考流块。",
        )

        focus_window_minutes: int = Field(
            default=30,
            ge=1,
            le=720,
            description="思考流焦点窗口（分钟）。last_focused_at 在此窗口内的活跃思考流被视为'当前焦点'，否则归入'背景在意'。",
        )

        curiosity_decay_half_life_hours: float = Field(
            default=12.0,
            ge=0.5,
            le=240.0,
            description="思考流 curiosity_score 的指数衰减半衰期（小时）。lazy 衰减：每次访问时按距 last_decay_at 的小时数衰减。",
        )

        curiosity_floor: float = Field(
            default=0.15,
            ge=0.0,
            le=0.9,
            description="思考流 curiosity_score 衰减下限。低于此值不再继续衰减。",
        )

        delta_marking: bool = Field(
            default=True,
            description="是否在 chatter 同步中给自上次以来 revision 增长的思考流加 🔄(刚推进) 标记。",
        )

    @config_section("runtime_sync")
    class RuntimeSyncSection(SectionBase):
        """life_chatter 同步层（注意力脑区）配置。"""

        latest_action_think_enabled: bool = Field(
            default=True,
            description="是否在 chatter transient 中注入当前 stream 最近一次 action-think 快照。",
        )

        recent_chat_enabled: bool = Field(
            default=True,
            description="是否在 chatter transient 中注入最近聊天记录快照。",
        )

        recent_chat_messages: int = Field(
            default=10,
            ge=0,
            le=50,
            description="最近聊天记录快照最多保留多少条。设为 0 表示关闭该块。",
        )

        salient_tail_enabled: bool = Field(
            default=True,
            description="是否在 chatter transient 中追加'最近关键活动'尾巴。关闭后不再从事件流派生活动摘要。",
        )

        salient_tail_max_items: int = Field(
            default=4,
            ge=1,
            le=20,
            description="最近关键活动最多保留的条目数。",
        )

        salient_tail_max_chars: int = Field(
            default=1000,
            ge=200,
            le=4000,
            description="最近关键活动总字符上限（超过则按时间倒序截断）。",
        )

        salient_tail_include_tool_failures: bool = Field(
            default=True,
            description="是否包含失败的工具结果。",
        )

        salient_tail_include_agent_results: bool = Field(
            default=True,
            description="是否包含 AGENT_RESULT（最新 1 条优先）。",
        )

        salient_tail_include_direct_messages: bool = Field(
            default=True,
            description="是否包含 dfc_message / direct_message / proactive_opportunity 类消息。",
        )

        salient_tail_include_inner_monologue: bool = Field(
            default=True,
            description="是否包含最近的 chatter_inner_monologue（最多 2 条）。",
        )

    settings: SettingsSection = Field(default_factory=SettingsSection)
    model: ModelSection = Field(default_factory=ModelSection)
    history_retrieval: HistoryRetrievalSection = Field(default_factory=HistoryRetrievalSection)
    web: WebSection = Field(default_factory=WebSection)
    snn: SNNSection = Field(default_factory=SNNSection)
    neuromod: NeuromodSection = Field(default_factory=NeuromodSection)
    dream: DreamSection = Field(default_factory=DreamSection)
    thresholds: ThresholdsSection = Field(default_factory=ThresholdsSection)
    memory_algorithm: MemoryAlgorithmSection = Field(default_factory=MemoryAlgorithmSection)
    chatter: ChatterSection = Field(default_factory=ChatterSection)
    multimodal: MultimodalSection = Field(default_factory=MultimodalSection)
    screen: ScreenSection = Field(default_factory=ScreenSection)
    streams: StreamsSection = Field(default_factory=StreamsSection)
    runtime_sync: RuntimeSyncSection = Field(default_factory=RuntimeSyncSection)
    drives: DrivesSection = Field(default_factory=DrivesSection)

    @field_validator("settings")
    @classmethod
    def validate_sleep_wake_times(cls, v: SettingsSection) -> SettingsSection:
        """验证睡眠/苏醒时间的格式和一致性。"""
        sleep_time = getattr(v, "sleep_time", "") or ""
        wake_time = getattr(v, "wake_time", "") or ""

        # 检查时间格式
        time_pattern = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')

        if sleep_time and not time_pattern.match(sleep_time):
            raise ValueError(
                f'sleep_time 格式必须是 HH:MM（24小时制），例如 "23:00"，收到: "{sleep_time}"'
            )

        if wake_time and not time_pattern.match(wake_time):
            raise ValueError(
                f'wake_time 格式必须是 HH:MM（24小时制），例如 "07:00"，收到: "{wake_time}"'
            )

        # 检查配对一致性
        sleep_set = bool(sleep_time.strip())
        wake_set = bool(wake_time.strip())

        if sleep_set != wake_set:
            raise ValueError(
                "sleep_time 和 wake_time 必须同时设置或同时留空"
            )

        if sleep_set and wake_set and sleep_time == wake_time:
            raise ValueError("sleep_time 和 wake_time 不能相同")

        return v
