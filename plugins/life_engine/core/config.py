"""life_engine 插件配置。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from pydantic import field_validator

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section

from ..constants import (
    DECAY_LAMBDA,
    DREAM_LEARNING_RATE,
    EXTERNAL_MESSAGE_ACTIVE_WINDOW_MINUTES,
    HEARTBEAT_IDLE_CRITICAL_THRESHOLD,
    HEARTBEAT_IDLE_WARNING_THRESHOLD,
    PRUNE_THRESHOLD,
    RRF_K,
    SPREAD_DECAY,
    SPREAD_THRESHOLD,
    TODO_URGENT_DAYS_THRESHOLD,
)

# 默认工作空间路径
_DEFAULT_WORKSPACE = str(Path(__file__).parent.parent.parent.parent / "data" / "life_engine_workspace")


class LifeEngineConfig(BaseConfig):
    """life_engine 插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "生命中枢最小原型配置"
    # 仅控制 WebUI 配置页暴露面；完整运行时/TOML 配置仍保留。
    __config_schema_visible_fields__: ClassVar[dict[str, set[str]]] = {
        "settings": {
            "enabled",
            "heartbeat_interval_seconds",
            "heartbeat_timeout_seconds",
            "sleep_time",
            "wake_time",
            "workspace_path",
            "max_rounds_per_heartbeat",
        },
        "model": {"task_name", "chatter_task_name"},
        "memory_index": {
            "enabled",
            "backend_enabled",
            "interval_seconds",
            "batch_size",
            "run_on_startup",
            "retry_failed",
            "reclaim_after_seconds",
        },
        "memory_witness": {
            "enabled",
            "interval_seconds",
            "model_task_name",
            "run_on_startup",
            "max_events_per_run",
            "timeout_seconds",
            "retry_delay_seconds",
            "max_witness_chars",
            "migrate_legacy_diaries",
            "legacy_diary_path",
        },
        "storage_local": {
            "database_path",
            "authority_state_path",
            "busy_timeout_seconds",
        },
        "shared_sync": {
            "enabled",
            "remote_host",
            "remote_port",
            "remote_database",
            "remote_user",
            "remote_password_env",
            "mysql_ssl_mode",
            "poll_interval_seconds",
            "batch_size",
            "push_enabled",
            "pull_enabled",
            "allowed_visibilities",
        },
        "memory_archive_sync": {
            "enabled",
            "remote_host",
            "remote_port",
            "remote_database",
            "remote_user",
            "remote_password_env",
            "mysql_ssl_mode",
            "mysql_ssl_ca",
            "mysql_ssl_cert",
            "mysql_ssl_key",
            "connect_timeout_seconds",
            "interval_seconds",
            "retry_max_seconds",
            "publish_batch_size",
            "publish_concurrency",
            "scan_batch_size",
            "max_batch_mib",
            "local_state_path",
        },
        "history_retrieval": {
            "enabled",
            "default_cross_stream",
            "adapter_signature",
        },
        "web": {
            "tavily_api_key",
            "tavily_api_keys",
            "tavily_base_url",
            "trust_env",
        },
        "chatter": {
            "enabled",
            "mode",
            "max_rounds_per_chat",
            "initial_history_messages",
            "recent_history_tail_messages",
            "router_context_projection_enabled",
            "router_context_projection_task_name",
            "router_context_projection_max_chars",
            "router_context_projection_poll_seconds",
            "router_context_projection_timeout_seconds",
            "subject_context_projection_task_name",
            "subject_context_projection_timeout_seconds",
            "enable_sub_agent",
            "sub_agent_task_name",
            "sub_agent_allow_mcp",
            "sub_agent_default_max_rounds",
            "enable_mcp",
        },
        "multimodal": {
            "enabled",
            "native_image",
            "native_emoji",
            "native_video",
            "native_audio",
            "max_images_per_payload",
        },
        "media_observer": {
            "enabled",
            "task_name",
        },
        "screen": {
            "enabled",
            "capture_method",
            "display",
            "native_when_available",
            "native_task_name",
            "fallback_task_name",
            "save_latest",
            "latest_path",
            "max_observation_chars",
        },
        "drives": {
            "enabled",
            "inject_to_heartbeat",
        },
        "streams": {
            "enabled",
            "max_active_streams",
            "inject_to_heartbeat",
            "sync_to_chatter",
        },
        "autonomy": {
            "enabled",
            "min_delay_minutes",
            "max_delay_minutes",
        },
        "curiosity": {
            "enabled",
            "inject_to_chatter",
            "task_name",
            "history_messages",
        },
        "runtime_sync": {
            "latest_action_think_enabled",
            "recent_chat_enabled",
            "recent_chat_messages",
            "trace_recent_changes_enabled",
            "trace_recent_changes_limit",
            "send_targets_enabled",
            "send_targets_limit",
            "send_targets_window_hours",
            "salient_tail_enabled",
        },
        "learning": {
            "enabled",
            "inject_to_heartbeat",
            "reflection_cooldown_minutes",
            "audit_interval_hours",
            "compress_trigger_count",
        },
        "orchestration": {
            "enabled",
            "max_concurrency",
            "max_mission_duration_seconds",
            "max_tokens_per_mission",
            "planner_task_name",
            "worker_task_name",
            "failure_policy",
            "trace_enabled",
        },
    }

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

        heartbeat_timeout_seconds: int = Field(
            default=120,
            description="单次心跳模型请求的超时（秒）。与心跳间隔解耦，慢模型可放宽。",
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

        subconscious_context_max_chars: int = Field(
            default=16000,
            ge=1000,
            description="潜意识上下文管理器的字符预算上限。",
        )

        subconscious_summary_max_chars: int = Field(
            default=4000,
            ge=200,
            description="潜意识规范摘要的最大字符数。",
        )

        subconscious_entry_max_chars: int = Field(
            default=480,
            ge=40,
            description="潜意识摘要单条目的最大字符数。",
        )

        subconscious_summary_max_entries: int = Field(
            default=60,
            ge=10,
            description="潜意识规范摘要保留的最大条目数，超出时裁剪保留最新。",
        )

        subconscious_recent_groups: int = Field(
            default=5,
            ge=0,
            description="潜意识上下文保留的最近完整因果组数量。",
        )

    @config_section("model")
    class ModelSection(SectionBase):
        """中枢模型任务设置。"""

        task_name: str = Field(
            default="core",
            description="潜意识（心跳）使用的模型任务名称，对应 config/model.toml 中的 [model_tasks.core]。",
        )
        chatter_task_name: str = Field(
            default="",
            description="主意识（chatter 表达层）使用的模型任务名。留空时跟随 task_name。",
        )

    @config_section("memory_index")
    class MemoryIndexSection(SectionBase):
        """异步 chunk 向量索引配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用独立的记忆 chunk 向量索引 worker。",
        )

        backend_enabled: bool = Field(
            default=True,
            description="是否初始化 Life Memory 的 Chroma 向量后端；关闭时保留 SQLite 全文检索。",
        )

        interval_seconds: int = Field(
            default=60,
            ge=30,
            description="索引 worker 每批之间的等待秒数。",
        )

        batch_size: int = Field(
            default=4,
            ge=1,
            le=50,
            description="每轮最多处理的 outbox 文档任务数。",
        )

        run_on_startup: bool = Field(
            default=True,
            description="启动后是否立即处理一批；关闭时先等待一个间隔。",
        )

        retry_failed: bool = Field(
            default=False,
            description="启动后的首批是否允许领取 failed 任务；默认关闭，且不会循环重试。",
        )

        reclaim_after_seconds: int = Field(
            default=600,
            ge=60,
            description="processing 任务超过该秒数后可由新 worker 回收。",
        )

    @config_section("memory_witness")
    class MemoryWitnessSection(SectionBase):
        """第一人称记忆见证意识配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用异步第一人称记忆见证意识。",
        )
        interval_seconds: int = Field(
            default=300,
            ge=60,
            description="见证意识定时苏醒的间隔秒数。",
        )
        model_task_name: str = Field(
            default="witness",
            description="见证意识使用的小模型任务名。",
        )
        run_on_startup: bool = Field(
            default=True,
            description="启动后是否立即回望一次尚未见证的经历。",
        )
        max_events_per_run: int = Field(
            default=500,
            ge=1,
            le=2000,
            description="每次苏醒最多读取的原始事件数（含操作噪音，游标推进用）。",
        )
        timeout_seconds: float = Field(
            default=120.0,
            ge=10.0,
            le=900.0,
            description="单次见证模型调用的外层超时秒数。",
        )
        retry_delay_seconds: int = Field(
            default=60,
            ge=10,
            le=1800,
            description="上游模型临时失败后再次尝试见证的等待秒数。",
        )
        max_witness_chars: int = Field(
            default=800,
            ge=80,
            le=4000,
            description="单篇第一人称见证正文的最大字符数。",
        )
        migrate_legacy_diaries: bool = Field(
            default=True,
            description="是否将旧 Diary Plugin 日记幂等登记为 legacy witness。",
        )
        legacy_diary_path: str = Field(
            default="data/diaries",
            description="旧日记只读迁移来源；原文件不会被删除或改写。",
        )

    @config_section("shared_sync")
    class SharedSyncSection(SectionBase):
        """离线优先共享事件同步；默认关闭且绝不从配置文件读取明文密码。"""

        enabled: bool = Field(
            default=False,
            description="是否启动共享事件同步 worker。默认关闭。",
        )
        remote_host: str = Field(default="", description="远端 MySQL 主机。")
        remote_port: int = Field(default=3306, ge=1, le=65535, description="远端 MySQL 端口。")
        remote_database: str = Field(default="elysium", description="共享账本数据库名。")
        remote_user: str = Field(default="", description="共享账本数据库用户。")
        remote_password_env: str = Field(
            default="ELYSIUM_SYNC_MYSQL_PASSWORD",
            description="保存远端 MySQL 密码的环境变量名；配置文件不保存密码。",
        )
        mysql_ssl_mode: str = Field(
            default="disabled",
            description="MySQL TLS 模式：disabled/required/verify-ca/verify-full。",
        )
        mysql_ssl_ca: str = Field(default="", description="可选 CA 证书路径。")
        mysql_ssl_cert: str = Field(default="", description="可选客户端证书路径。")
        mysql_ssl_key: str = Field(default="", description="可选客户端私钥路径。")
        connect_timeout_seconds: int = Field(
            default=5,
            ge=1,
            le=60,
            description="远端连接超时秒数。",
        )
        poll_interval_seconds: float = Field(
            default=1.0,
            ge=0.1,
            le=300.0,
            description="同步轮询间隔秒数。",
        )
        batch_size: int = Field(
            default=100,
            ge=1,
            le=1000,
            description="每轮最大推送或拉取事件数。",
        )
        lease_seconds: float = Field(
            default=60.0,
            ge=1.0,
            le=3600.0,
            description="Outbox 投递租约秒数；进程崩溃后可自动回收。",
        )
        base_backoff_seconds: float = Field(
            default=1.0,
            ge=0.0,
            le=60.0,
            description="失败后的初始重试退避秒数。",
        )
        max_backoff_seconds: float = Field(
            default=300.0,
            ge=1.0,
            le=3600.0,
            description="重试退避上限秒数。",
        )
        push_enabled: bool = Field(default=True, description="是否推送明确授权共享的本地事件。")
        pull_enabled: bool = Field(
            default=False,
            description="是否拉取远端事件；应用投影器完成前保持关闭。",
        )
        allowed_visibilities: list[str] = Field(
            default_factory=lambda: ["shared"],
            description="允许跨边界传输的 visibility 白名单。",
        )
        consumer_id: str = Field(
            default="life_engine.shared_sync",
            description="远端事件应用游标的消费者 ID。",
        )

    @config_section("storage_local")
    class StorageLocalSection(SectionBase):
        """Local backend and single-host authority control plane."""

        database_path: str = Field(
            default="data/life_storage/local.sqlite3",
            description="New managed path; existing source databases are never overwritten.",
        )
        authority_state_path: str = Field(
            default="data/life_storage/authority.json",
            description="Hash-chained local authority registry state.",
        )
        busy_timeout_seconds: int = Field(
            default=10,
            ge=1,
            le=300,
            description="Bounded SQLite lock wait timeout.",
        )

    @config_section("memory_archive_sync")
    class MemoryArchiveSyncSection(SectionBase):
        """Owner-private, local-first archive of all technical memory stores."""

        enabled: bool = Field(
            default=False,
            description="Enable the unified memory archive worker. Disabled by default.",
        )
        remote_host: str = Field(default="", description="Remote MySQL host.")
        remote_port: int = Field(
            default=3306,
            ge=1,
            le=65535,
            description="Remote MySQL port.",
        )
        remote_database: str = Field(
            default="elysium",
            description="Remote archive database name.",
        )
        remote_user: str = Field(default="", description="Remote archive user.")
        remote_password_env: str = Field(
            default="ELYSIUM_MEMORY_ARCHIVE_MYSQL_PASSWORD",
            description="Environment variable containing the password; never plaintext TOML.",
        )
        mysql_ssl_mode: str = Field(
            default="disabled",
            description="MySQL TLS mode: disabled/required/verify-ca/verify-full.",
        )
        mysql_ssl_ca: str = Field(default="", description="Optional CA path.")
        mysql_ssl_cert: str = Field(
            default="",
            description="Optional client certificate path.",
        )
        mysql_ssl_key: str = Field(
            default="",
            description="Optional client private-key path.",
        )
        connect_timeout_seconds: int = Field(
            default=5,
            ge=1,
            le=60,
            description="Remote connection timeout in seconds.",
        )
        interval_seconds: float = Field(
            default=300.0,
            ge=10.0,
            le=86400.0,
            description="Interval between incremental archive scans.",
        )
        retry_max_seconds: float = Field(
            default=900.0,
            ge=5.0,
            le=86400.0,
            description="Maximum retry backoff while MySQL is unavailable.",
        )
        publish_batch_size: int = Field(
            default=250,
            ge=1,
            le=1000,
            description="Maximum records per remote transaction.",
        )
        publish_concurrency: int = Field(
            default=2,
            ge=1,
            le=6,
            description="Maximum concurrent remote transactions.",
        )
        scan_batch_size: int = Field(
            default=500,
            ge=1,
            le=5000,
            description="Maximum local records inspected per scan step.",
        )
        max_batch_mib: int = Field(
            default=4,
            ge=1,
            le=32,
            description="Maximum estimated payload per remote transaction.",
        )
        local_state_path: str = Field(
            default=".memory/archive_sync_state.sqlite3",
            description="Rebuildable state path relative to the workspace.",
        )

    @config_section("autonomy")
    class AutonomySection(SectionBase):
        """自主意向循环配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 life_engine 自主意向登记与到点浮现。",
        )

        min_delay_minutes: int = Field(
            default=1,
            ge=1,
            description="自主意向允许的最小延迟分钟数。",
        )

        max_delay_minutes: int = Field(
            default=1440,
            ge=1,
            description="自主意向允许的最大延迟分钟数。",
        )

        show_targets_in_heartbeat: bool = Field(
            default=True,
            description="是否在心跳 prompt 中呈现可触达的发送目标列表（主动性的行动空间）。",
        )

    @config_section("narrative")
    class NarrativeSection(SectionBase):
        """沉淀器（长河→自我叙事）配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用沉淀器：低频回望长河，由她自己写下叙事。",
        )

        inject_to_heartbeat: bool = Field(
            default=True,
            description="是否在心跳 prompt 中呈现「回望长河」段落（仅在到期时出现）。",
        )

        min_interval_hours: float = Field(
            default=20.0,
            ge=1.0,
            description="两次沉淀之间的最小间隔小时数——回望是低频的，不是每次心跳的作业。",
        )

        min_moments: int = Field(
            default=3,
            ge=1,
            description="长河中至少累积多少条未沉淀留痕，才呈现回望邀请。",
        )

        invite_cooldown_hours: float = Field(
            default=6.0,
            ge=0.5,
            description="回望邀请呈现后，多少小时内不再重复呈现（她有不回应的自由）。",
        )

        max_moments_shown: int = Field(
            default=12,
            ge=1,
            le=50,
            description="回望段落最多摆出多少条未沉淀留痕。",
        )

    @config_section("learning")
    class LearningSection(SectionBase):
        """三环自学习/自反思系统配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用三环自学习系统（快环反思/审计环/慢环压缩）。",
        )

        inject_to_heartbeat: bool = Field(
            default=True,
            description="是否在心跳 prompt 中注入自我认知文档和学习进展。",
        )

        reflection_cooldown_minutes: float = Field(
            default=30.0,
            ge=5.0,
            description="快环反思冷却时间（分钟）。两次反思之间的最小间隔。",
        )

        audit_interval_hours: float = Field(
            default=6.0,
            ge=1.0,
            description="审计环执行间隔（小时）。有待审洞察时，至少间隔这么久才再次审计。",
        )

        audit_batch_size: int = Field(
            default=3,
            ge=1,
            le=10,
            description="每次审计最多处理几条洞察。",
        )

        compress_trigger_count: int = Field(
            default=5,
            ge=2,
            description="积累多少条 validated 洞察后触发慢环压缩。",
        )

        compress_interval_hours: float = Field(
            default=48.0,
            ge=6.0,
            description="两次慢环压缩之间的最小间隔（小时）。",
        )

        subject_review_enabled: bool = Field(
            default=True,
            description=(
                "是否提供 SOUL/USER/MEMORY 的低频复盘机会。"
                "这只控制邀请，不授权后台改写。"
            ),
        )

        subject_review_soul_interval_hours: float = Field(
            default=720.0,
            ge=24.0,
            description="SOUL.md 两次复盘时间点之间的工程间隔（小时）。",
        )

        subject_review_user_interval_hours: float = Field(
            default=720.0,
            ge=24.0,
            description="USER.md 两次复盘时间点之间的工程间隔（小时）。",
        )

        subject_review_memory_interval_hours: float = Field(
            default=168.0,
            ge=24.0,
            description="MEMORY.md 两次复盘时间点之间的工程间隔（小时）。",
        )

        subject_review_offer_cooldown_hours: float = Field(
            default=24.0,
            ge=1.0,
            description="复盘机会出现后至少多久不重复呈现（小时）。",
        )

        knowledge_max_chars: int = Field(
            default=2000,
            ge=500,
            le=6000,
            description="注入 prompt 的自我认知文档最大字符数。",
        )

        skill_distill_trigger_count: int = Field(
            default=3,
            ge=1,
            le=10,
            description="积累多少条 validated 技能类洞察后触发蒸馏。",
        )

        skill_distill_interval_hours: float = Field(
            default=24.0,
            ge=6.0,
            description="两次技能蒸馏之间的最小间隔（小时）。",
        )

        skill_catalog_max_chars: int = Field(
            default=600,
            ge=200,
            le=2000,
            description="注入 prompt 的技能目录（L1）最大字符数。",
        )

        skill_max_edits: int = Field(
            default=2,
            ge=1,
            le=5,
            description="旧配置兼容字段；认知修改范围现在由独立整合与审视过程决定。",
        )

    @config_section("curiosity")
    class CuriositySection(SectionBase):
        """认知机会候选生成与投影配置（保留旧 section 名兼容）。"""

        enabled: bool = Field(
            default=True,
            description="是否启用外部认知机会候选生成器。",
        )

        inject_to_chatter: bool = Field(
            default=True,
            description="是否将有来源的认知机会候选投影注入 life_chatter suffix。",
        )

        inject_to_heartbeat: bool = Field(
            default=True,
            description="是否将认知机会候选投影注入心跳 prompt；候选不代表主体决定。",
        )

        task_name: str = Field(
            default="",
            description="认知机会候选生成器使用的模型任务名。留空时跟随 [model].task_name。",
        )

        history_messages: int = Field(
            default=20,
            ge=0,
            le=80,
            description="生成候选时携带的最近统一聊天历史条数。",
        )

        timeout_seconds: float = Field(
            default=30.0,
            ge=3.0,
            le=120.0,
            description="单次认知机会候选生成的 LLM 超时秒数。",
        )

        max_prompt_chars: int = Field(
            default=1200,
            ge=200,
            le=4000,
            description="兼容字段：认知机会 Prompt 投影的 UTF-8 硬字节预算。",
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
            description="兼容字段名；conversation_evidence 的 search 每页全局最多扫描多少条历史消息。",
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

        core_max_result_bytes: int = Field(
            default=8192,
            ge=2048,
            le=65536,
            description="core/heartbeat 单次对话证据投影的 UTF-8 硬字节上限。",
        )

        chat_max_result_bytes: int = Field(
            default=16384,
            ge=2048,
            le=65536,
            description="chat 单次对话证据投影的 UTF-8 硬字节上限。",
        )

        voice_max_result_bytes: int = Field(
            default=8192,
            ge=2048,
            le=65536,
            description="voice_live 单次对话证据投影的 UTF-8 硬字节上限。",
        )

        livestream_max_result_bytes: int = Field(
            default=8192,
            ge=2048,
            le=65536,
            description="livestream 单次对话证据投影的 UTF-8 硬字节上限。",
        )

        minecraft_max_result_bytes: int = Field(
            default=8192,
            ge=2048,
            le=65536,
            description="minecraft 单次对话证据投影的 UTF-8 硬字节上限。",
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

        trust_env: bool = Field(
            default=True,
            description="是否信任系统代理环境变量（HTTP_PROXY/HTTPS_PROXY）。关闭后 Tavily 请求始终直连。",
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

        context_compression_max_groups: int = Field(
            default=12,
            ge=1,
            description="上下文压缩摘要最多保留的旧对话组数。",
        )

        context_compression_max_part_chars: int = Field(
            default=360,
            ge=16,
            description="上下文压缩摘要中每个内容片段的最大字符数。",
        )

        rolling_context_snapshot_char_budget: int = Field(
            default=320_000,
            ge=1_000,
            description="life_chatter 滚动上下文快照的序列化硬上限字符预算。",
        )

        context_compaction_enabled: bool = Field(
            default=True,
            description="是否启用 life_chatter 分层运行态/快照上下文压缩。",
        )

        context_compaction_trigger_chars: int = Field(
            default=120_000,
            ge=1_000,
            description="运行态上下文超过该序列化字符估计值时触发分层压缩。",
        )

        context_compaction_target_chars: int = Field(
            default=80_000,
            ge=500,
            description="分层压缩后的目标序列化字符估计值（应不大于 trigger）。",
        )

        context_compaction_min_recent_groups: int = Field(
            default=2,
            ge=1,
            description="压缩后至少保留的最近完整对话组数（未闭合工具链额外保护）。",
        )

        context_compaction_summary_max_chars: int = Field(
            default=12_000,
            ge=200,
            description="规范 summary 正文最大字符数；旧 summary 更新替换且不嵌套。",
        )

        recent_history_tail_messages: int = Field(
            default=0,
            ge=0,
            description=(
                "兼容旧配置：若 initial_history_messages 未显式配置且此值 > 0，"
                "则回退使用该值作为首轮历史消息条数。"
            ),
        )

        router_context_projection_enabled: bool = Field(
            default=True,
            description=(
                "是否为对话 Router 启用可追溯、可重建的轻量人格/记忆投影。"
                "只影响 Router 输入，不替换表达层的完整人格与记忆。"
            ),
        )

        router_context_projection_task_name: str = Field(
            default="router_context_projection",
            description="生成 Router 上下文投影时使用的云端模型任务名。",
        )

        router_context_projection_max_chars: int = Field(
            default=6000,
            ge=500,
            le=20000,
            description="单个 Router 上下文投影正文允许的最大字符数。",
        )

        router_context_projection_poll_seconds: float = Field(
            default=1.0,
            ge=0.2,
            le=60.0,
            description="检测 SOUL.md、USER.md、MEMORY.md 外部变更的轮询间隔。",
        )

        router_context_projection_timeout_seconds: float = Field(
            default=90.0,
            ge=5.0,
            le=300.0,
            description="单个云端模型生成 Router 上下文投影的超时时间。",
        )

        subject_context_projection_task_name: str = Field(
            default="router_context_projection",
            description=(
                "Cloud-model task used to build on-demand, bounded subject "
                "projections from SOUL.md, USER.md and MEMORY.md."
            ),
        )

        subject_context_projection_timeout_seconds: float = Field(
            default=90.0,
            ge=5.0,
            le=300.0,
            description="Timeout for one subject-context projection model attempt.",
        )

        enable_sub_agent: bool = Field(
            default=False,
            description=(
                "是否允许 life_chatter 调用子代理（life_run_agent）。"
                "开启后 life_chatter 表达层可以把复杂多步任务委托给独立子代理执行，"
                "子代理拥有独立 LLM 上下文，支持同步等待或后台运行。"
            ),
        )

        sub_agent_task_name: str = Field(
            default="agent",
            description=(
                "子代理创建 LLM request 时使用的模型任务名，对应 config/model.toml 中的 task key。"
                "留空时回退为 agent。"
            ),
        )

        sub_agent_allow_mcp: bool = Field(
            default=True,
            description=(
                "子代理是否可以使用 MCP 工具。"
                "开启后 life_run_agent 的 mcp_servers 参数生效，可把指定 MCP 服务器能力委托给子代理。"
            ),
        )

        sub_agent_default_max_rounds: int = Field(
            default=8,
            ge=1,
            le=30,
            description="life_run_agent 默认最大工具调用轮数；调用时可被 max_rounds 参数覆盖。",
        )

        enable_mcp: bool = Field(
            default=True,
            description=(
                "是否允许 life_chatter 主代理直接看到非 defer_loading 的 MCP 工具。"
                "关闭后所有 MCP 工具都不会出现在 life_chatter 的工具列表里，"
                "需要通过 life_run_agent 委托给子代理使用。"
            ),
        )

    @config_section("multimodal")
    class MultimodalSection(SectionBase):
        """life_chatter 原生多模态输入配置。

        图片和表情包默认走原生视觉输入；音频、视频需要模型显式声明对应能力后再开启。
        原始媒体只在当前对话轮保留，轮次结束后转换为安全描述，避免后续请求反复携带
        base64 数据。
        """

        enabled: bool = Field(
            default=True,
            description="启用 life_chatter 原生多模态输入。",
        )
        native_image: bool = Field(
            default=True,
            description="是否把 image 媒体作为原生 Image Content 注入。",
        )
        native_emoji: bool = Field(
            default=True,
            description="是否把 emoji / 表情包媒体作为原生 Image Content 注入。",
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
            default=False,
            description=(
                "兼容旧配置项，不再控制媒体生命周期。原生 Image/Audio/Video 只在当前对话轮保留，"
                "进入 WAIT_USER 后统一转换为安全描述并释放原始数据。"
            ),
        )
        unsupported_audio_placeholder: str = Field(
            default="[语音消息]",
            description="未知/不支持的音频格式（如 silk/amr）降级为该文本占位。",
        )

    @config_section("media_observer")
    class MediaObserverSection(SectionBase):
        """life_chatter 按需观察图片/视频/语音的专用子代理配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 life_chatter 的按需媒体观察工具。",
        )
        task_name: str = Field(
            default="vision",
            description="兼容旧配置保留；当前 inspect_media 不再调用媒体观察子代理模型。",
        )
        fallback_task_name: str = Field(
            default="agent",
            description="兼容旧配置保留；当前 inspect_media 不再调用降级整合模型。",
        )
        max_image_bytes: int = Field(
            default=12 * 1024 * 1024,
            ge=1,
            description="单张图片/表情包允许传给原生观察模型的最大字节数。",
        )
        max_audio_bytes: int = Field(
            default=30 * 1024 * 1024,
            ge=1,
            description="单段音频允许传给原生观察模型的最大字节数。",
        )
        max_video_bytes: int = Field(
            default=200 * 1024 * 1024,
            ge=1,
            description="单个视频允许传给原生观察模型或降级摘要链路的最大字节数。",
        )
        fallback_video_frames: int = Field(
            default=4,
            ge=1,
            le=12,
            description="视频不走原生输入时，降级抽取的最大关键帧数量。",
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
            default="vision",
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
            description="是否在 chatter transient 中注入当前 stream 最近一次独白/思考快照。",
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

        trace_recent_changes_enabled: bool = Field(
            default=True,
            description="是否在 chatter suffix 中注入最近文件修改追溯。",
        )

        trace_recent_changes_limit: int = Field(
            default=3,
            ge=0,
            le=10,
            description="最近文件修改追溯最多展示多少条。设为 0 表示关闭该块。",
        )

        send_targets_enabled: bool = Field(
            default=True,
            description="是否在 chatter suffix 中注入近期可发送目标列表。",
        )

        send_targets_limit: int = Field(
            default=8,
            ge=1,
            le=20,
            description="近期可发送目标列表最多展示多少个聊天流。",
        )

        send_targets_window_hours: float = Field(
            default=24.0,
            ge=0.1,
            le=168.0,
            description="近期可发送目标列表的活跃窗口，单位小时。",
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
            description="是否包含 dfc_message / direct_message / inner_dialogue / proactive_opportunity 类消息。",
        )

        salient_tail_include_inner_monologue: bool = Field(
            default=True,
            description="是否包含最近的 chatter_inner_monologue（最多 2 条）。",
        )

    @config_section("orchestration")
    class OrchestrationSection(SectionBase):
        """子代理编排系统配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用子代理编排系统（life_dispatch_mission）。",
        )

        max_concurrency: int = Field(
            default=4,
            ge=1,
            le=8,
            description="最大并行 worker 数。",
        )

        default_max_rounds: int = Field(
            default=12,
            ge=1,
            le=30,
            description="每个 worker 默认最大工具调用轮数。",
        )

        default_timeout_seconds: int = Field(
            default=300,
            ge=30,
            le=1800,
            description="每个 worker 默认超时秒数。",
        )

        max_mission_duration_seconds: int = Field(
            default=1800,
            ge=60,
            le=7200,
            description="整个使命的全局超时秒数。",
        )

        max_tokens_per_mission: int = Field(
            default=200_000,
            ge=10_000,
            le=2_000_000,
            description="每个使命的全局 token 预算上限。",
        )

        max_tasks_per_mission: int = Field(
            default=12,
            ge=1,
            le=30,
            description="每个使命最多包含的子任务数。",
        )

        planner_task_name: str = Field(
            default="agent",
            description="规划器使用的模型任务名。",
        )

        worker_task_name: str = Field(
            default="agent",
            description="Worker 使用的模型任务名。",
        )

        retry_max_attempts: int = Field(
            default=2,
            ge=0,
            le=5,
            description="任务失败后最大重试次数。",
        )

        retry_backoff_base: float = Field(
            default=2.0,
            ge=1.0,
            le=10.0,
            description="重试指数退避底数。",
        )

        failure_policy: str = Field(
            default="continue_others",
            description="部分失败策略: fail_fast / continue_others / retry_then_skip。",
        )

        trace_enabled: bool = Field(
            default=True,
            description="是否写入编排追踪文件。",
        )

    @config_section("minecraft")
    class MinecraftSection(SectionBase):
        """Minecraft 具身体验配置。"""

        enabled: bool = Field(
            default=False,
            description="是否启用 Minecraft 具身体验。",
        )

        java_path: str = Field(
            default="java",
            description="Java 可执行文件路径。",
        )

        mc_version: str = Field(
            default="1.21.1",
            description="Minecraft 版本。",
        )

        world_name: str = Field(
            default="Elysian Realm",
            description="专用存档名称。",
        )

        mc_home: str = Field(
            default="/mnt/g/Game/Minecraft/.minecraft",
            description="Exact WSL path of the managed Minecraft home.",
        )

        launch_bat: str = Field(
            default=r"G:\Game\Minecraft\PCL\LaunchElysia.bat",
            description="Exact Windows launch script for the managed client.",
        )

        launch_dir: str = Field(
            default=r"G:\Game\Minecraft\PCL",
            description="Exact Windows working directory for the launch script.",
        )

        window_width: int = Field(
            default=1280,
            ge=640,
            le=3840,
            description="游戏窗口宽度。",
        )

        window_height: int = Field(
            default=720,
            ge=360,
            le=2160,
            description="游戏窗口高度。",
        )

        consciousness_interval_seconds: float = Field(
            default=6.0,
            ge=2.0,
            le=30.0,
            description="意识层决策间隔（秒）。",
        )

        vla_model: str = Field(
            default="bytedance-research/UI-TARS-7B-SFT",
            description="VLA 模型名称或路径。",
        )

        vla_fps: int = Field(
            default=6,
            ge=1,
            le=30,
            description="VLA 闭环帧率。",
        )

        vla_timeout_seconds: float = Field(
            default=30.0,
            ge=5.0,
            le=120.0,
            description="单个意图最大执行时间（秒）。",
        )

        max_session_minutes: int = Field(
            default=60,
            ge=5,
            le=240,
            description="最大会话时长（分钟）。",
        )

        reflex_enabled: bool = Field(
            default=True,
            description="是否启用 Reflex 保护层。",
        )

        offline_username: str = Field(
            default="Elysia",
            description="离线模式用户名。",
        )

        default_body: str = Field(
            default="agent",
            pattern=r"^(agent|bot|biomimetic)$",
            description="Explicit Minecraft body selected when start omits body_name.",
        )

        agent_bridge_uri: str = Field(
            default="ws://host.docker.internal:8765/elysium",
            description="Authenticated NeoForge executor bridge URI.",
        )

        agent_bridge_listen_uri: str | None = Field(
            default="ws://127.0.0.1:18765/elysium",
            description="WSL listener for the outbound Windows agent relay.",
        )

        agent_token_file: str = Field(
            default="/mnt/g/Game/Minecraft/.minecraft/config/elysium_bridge.json",
            description="NeoForge bridge configuration containing its generated token.",
        )

        biomimetic_bridge_uri: str = Field(
            default="ws://host.docker.internal:8766/elysium",
            description="Authenticated first-person native-input sidecar URI.",
        )

        biomimetic_bridge_listen_uri: str | None = Field(
            default="ws://127.0.0.1:18766/elysium",
            description="WSL listener for the outbound Windows native-body relay.",
        )

        biomimetic_token_file: str = Field(
            default="/mnt/g/Game/Minecraft/.minecraft/config/elysium_native_bridge.json",
            description="Native sidecar configuration containing its generated token.",
        )

        bot_bridge_uri: str = Field(
            default="ws://127.0.0.1:18767/elysium",
            description="Fallback URI for the headless bot body bridge.",
        )

        bot_bridge_listen_uri: str | None = Field(
            default="ws://127.0.0.1:18767/elysium",
            description="WSL listener for the outbound headless bot body relay.",
        )

        bot_token_file: str = Field(
            default="minecraft/bot_bridge_token.json",
            description="Workspace-relative token file generated for the bot body.",
        )

        bot_server_host: str = Field(
            default="auto",
            min_length=1,
            description=(
                "Minecraft server or LAN host the bot body joins; \"auto\" "
                "resolves the WSL default gateway at launch time."
            ),
        )

        bot_server_port: int = Field(
            default=25565,
            ge=1,
            le=65535,
            description="Minecraft server or LAN port the bot body joins.",
        )

        bot_username: str = Field(
            default="Elysia",
            pattern=r"^[A-Za-z0-9_]{1,16}$",
            description=(
                "In-game account name for the headless bot body; must differ "
                "from the human player's account name."
            ),
        )

        bot_observation_interval_ms: int = Field(
            default=1000,
            gt=0,
            description="Bot observation snapshot cadence in milliseconds.",
        )

        bot_entity_radius_blocks: int = Field(
            default=32,
            gt=0,
            description="Bot entity sensor radius in blocks.",
        )

        shared_world_enabled: bool = Field(
            default=True,
            description=(
                "Her own client window joins the human player's LAN world, "
                "giving her a true first-person view; disabled falls back to "
                "the configured singleplayer world."
            ),
        )

        agent_shared_username: str = Field(
            default="Elysia",
            pattern=r"^[A-Za-z0-9_]{1,16}$",
            description=(
                "In-game account name of her own client in the shared world; "
                "must differ from the human player's account name."
            ),
        )

        game_turn_interval_seconds: int = Field(
            default=5,
            ge=1,
            description=(
                "Her continuous play cadence while a Minecraft session is "
                "active; the heartbeat loop accelerates to this interval so "
                "she keeps playing instead of waiting between chat turns."
            ),
        )

        planner_task_name: str = Field(
            default="agent",
            description="Configured Elysium model task used for game execution planning.",
        )

        bridge_ready_timeout_seconds: float = Field(
            default=240.0,
            gt=0,
            description="Technical launch deadline for the selected body endpoint.",
        )

        world_ready_timeout_seconds: float = Field(
            default=120.0,
            gt=0,
            description="Deadline for a playable world and advancing observations.",
        )

        require_quick_play: bool = Field(
            default=True,
            description="Require the launch script to enter the exact configured world.",
        )

        expected_bridge_version: str = Field(
            default="0.2.1",
            min_length=1,
            description="Exact authenticated NeoForge bridge build version.",
        )

        bridge_mod_filename: str = Field(
            default="elysium_bridge-0.2.1.jar",
            min_length=1,
            description="Exact selected NeoForge bridge artifact filename.",
        )

        expected_bridge_sha256: str = Field(
            default=(
                "F6B80E166F8C3EDA683020C8154D817DA3098873AE9ECDF6161F05C8FF8A50DC"
            ),
            pattern=r"^[A-Fa-f0-9]{64}$",
            description="Pinned SHA-256 for the selected NeoForge bridge artifact.",
        )

        baritone_mod_filename: str = Field(
            default="baritone-unoptimized-neoforge-1.11.2.jar",
            min_length=1,
            description="Exact official Baritone NeoForge artifact filename.",
        )

        expected_baritone_sha256: str = Field(
            default=(
                "B413CE0A2754A3C8484AAE39875CF84BE1F999DEE208E86D41B3D0D329D5CA35"
            ),
            pattern=r"^[A-Fa-f0-9]{64}$",
            description="Pinned SHA-256 for the official Baritone artifact.",
        )

        intent_timeout_seconds: float | None = Field(
            default=300.0,
            gt=0,
            description="Optional caller-owned lifetime for one game intention.",
        )

        @field_validator("intent_timeout_seconds", mode="before")
        @classmethod
        def normalize_disabled_intent_timeout(cls, value: object) -> object:
            """Treat TOML's generated zero sentinel as an unset timeout."""

            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value) == 0.0
            ):
                return None
            return value

    settings: SettingsSection = Field(default_factory=SettingsSection)
    model: ModelSection = Field(default_factory=ModelSection)
    memory_index: MemoryIndexSection = Field(default_factory=MemoryIndexSection)
    memory_witness: MemoryWitnessSection = Field(default_factory=MemoryWitnessSection)
    storage_local: StorageLocalSection = Field(default_factory=StorageLocalSection)
    shared_sync: SharedSyncSection = Field(default_factory=SharedSyncSection)
    memory_archive_sync: MemoryArchiveSyncSection = Field(
        default_factory=MemoryArchiveSyncSection
    )
    curiosity: CuriositySection = Field(default_factory=CuriositySection)
    history_retrieval: HistoryRetrievalSection = Field(default_factory=HistoryRetrievalSection)
    web: WebSection = Field(default_factory=WebSection)
    thresholds: ThresholdsSection = Field(default_factory=ThresholdsSection)
    memory_algorithm: MemoryAlgorithmSection = Field(default_factory=MemoryAlgorithmSection)
    chatter: ChatterSection = Field(default_factory=ChatterSection)
    multimodal: MultimodalSection = Field(default_factory=MultimodalSection)
    media_observer: MediaObserverSection = Field(default_factory=MediaObserverSection)
    screen: ScreenSection = Field(default_factory=ScreenSection)
    streams: StreamsSection = Field(default_factory=StreamsSection)
    runtime_sync: RuntimeSyncSection = Field(default_factory=RuntimeSyncSection)
    drives: DrivesSection = Field(default_factory=DrivesSection)
    minecraft: MinecraftSection = Field(default_factory=MinecraftSection)
    orchestration: OrchestrationSection = Field(default_factory=OrchestrationSection)
    autonomy: AutonomySection = Field(default_factory=AutonomySection)
    narrative: NarrativeSection = Field(default_factory=NarrativeSection)
    learning: LearningSection = Field(default_factory=LearningSection)

    @field_validator("chatter")
    @classmethod
    def validate_context_compaction_budgets(cls, v: LifeEngineConfig.ChatterSection):
        """Ensure compaction target <= trigger and snapshot hard cap is usable."""
        trigger = int(getattr(v, "context_compaction_trigger_chars", 0) or 0)
        target = int(getattr(v, "context_compaction_target_chars", 0) or 0)
        hard = int(getattr(v, "rolling_context_snapshot_char_budget", 0) or 0)
        if trigger > 0 and target > trigger:
            raise ValueError(
                "context_compaction_target_chars 不能大于 context_compaction_trigger_chars"
            )
        if hard > 0 and trigger > hard:
            # Allow but clamp is not done here; warn via ValueError only if clearly broken.
            pass
        return v

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
