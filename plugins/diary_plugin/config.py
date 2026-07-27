"""日记插件配置类。"""

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


@config_section("plugin")
class PluginSection(SectionBase):
    """插件主配置。"""

    enabled: bool = Field(default=True, description="是否启用日记插件")
    inject_system_prompt: bool = Field(
        default=True,
        description="是否向 actor bucket 注入系统提示语",
    )
    inherit_default_chatter_persona_prompt: bool = Field(
        default=True,
        description="自动写日记和连续记忆压缩是否复用 default_chatter 的完整系统人设提示词",
    )
    strict_identity_name_lock: bool = Field(
        default=True,
        description="是否启用严格名字锁定，只有完全匹配核心昵称才视为本体，避免相似名字混淆",
    )


@config_section("storage")
class StorageSection(SectionBase):
    """日记存储配置。"""

    base_path: str = Field(
        default="data/diaries",
        description="日记存储根目录",
    )
    date_format: str = Field(
        default="%Y-%m",
        description="日期目录格式（用于月份目录）",
    )
    file_format: str = Field(
        default="%Y-%m-%d.md",
        description="日记文件名格式",
    )


@config_section("format")
class FormatSection(SectionBase):
    """日记格式配置。"""

    enable_header: bool = Field(
        default=True,
        description="是否在日记开头添加基本信息头",
    )
    enable_section: bool = Field(
        default=True,
        description="是否启用时间段分类（上午/下午/晚上）",
    )
    time_format: str = Field(
        default="%H:%M",
        description="时间戳格式",
    )
    default_section: str = Field(
        default="其他",
        description="默认时间段分类",
    )


@config_section("dedup")
class DedupSection(SectionBase):
    """去重配置。"""

    enabled: bool = Field(
        default=True,
        description="是否启用写前重复检查",
    )
    similarity_threshold: float = Field(
        default=0.8,
        description="相似度阈值（超过此值视为重复）",
    )
    min_content_length: int = Field(
        default=5,
        description="最小内容长度（短于此长度不进行去重检查）",
    )


@config_section("reminder")
class ReminderSection(SectionBase):
    """System Reminder 配置。"""

    bucket: str = Field(
        default="actor",
        description="System Reminder 注入的 bucket",
    )
    name: str = Field(
        default="关于写日记",
        description="System Reminder 名称",
    )
    custom_instructions: str = Field(
        default="",
        description="自定义引导语（会追加到默认引导语后面）",
    )


@config_section("auto_diary")
class AutoDiarySection(SectionBase):
    """自动写日记配置。"""

    enabled: bool = Field(
        default=True,
        description="是否启用自动写日记功能",
    )
    message_threshold: int = Field(
        default=20,
        description="触发自动写日记的消息数量阈值（同时作为总结的消息条数）",
    )
    summary_timeout_seconds: int = Field(
        default=120,
        description="自动日记后台总结任务的最大执行秒数",
    )
    allow_group_chat: bool = Field(
        default=False,
        description="是否允许群聊自动写日记（False=仅私聊触发）",
    )
    inject_runtime_user_prompt_once: bool = Field(
        default=True,
        description="自动写日记成功后，是否将摘要一次性注入到下一次 user prompt 的 extra 字段",
    )
    runtime_user_prompt_target_names: list[str] = Field(
        default_factory=lambda: ["default_chatter_user_prompt"],
        description="允许注入自动日记摘要的一次性 user prompt 模板名列表",
    )
    runtime_user_prompt_prefix: str = Field(
        default="【自动日记摘要】",
        description="一次性注入到 user prompt 时的前缀",
    )
    runtime_user_prompt_guidance: str = Field(
        default="可将其视为前面对话的小总结，知道发生了什么即可，不必强制引用其中表述。",
        description="一次性注入到 user prompt 时附带的使用提示",
    )
    runtime_user_prompt_max_length: int = Field(
        default=240,
        description="一次性注入摘要的最大长度（超出会截断）",
    )


@config_section("model")
class ModelSection(SectionBase):
    """模型配置。"""

    task_name: str = Field(
        default="diary",
        description="写日记使用的任务模型名称（对应 model.toml 中的 [model_tasks.xxx]）",
    )


@config_section("continuous_memory")
class ContinuousMemorySection(SectionBase):
    """连续记忆配置。"""

    enabled: bool = Field(
        default=False,
        description="是否启用按聊天隔离的连续记忆空间",
    )
    base_path: str = Field(
        default="data/continuous_memories",
        description="连续记忆存储根目录",
    )
    private_subdir: str = Field(
        default="private",
        description="私聊连续记忆子目录",
    )
    group_subdir: str = Field(
        default="group",
        description="群聊连续记忆子目录",
    )
    discuss_subdir: str = Field(
        default="discuss",
        description="讨论组连续记忆子目录",
    )
    batch_size: int = Field(
        default=5,
        description="每累计多少个自动写出的新日记项触发一次压缩",
    )
    max_levels: int = Field(
        default=3,
        description="最大连续记忆压缩层级（L1-L3）",
    )
    max_items_top_level: int = Field(
        default=0,
        description="最高压缩层（Lmax_levels）最大保留条目数（0=不限制，超出时会裁剪最旧条目）",
    )
    inject_prompt: bool = Field(
        default=True,
        description="是否将当前聊天流的连续记忆动态注入主回复 prompt 的专用 continuous_memory 区块",
    )
    include_recent_entries_in_prompt: bool = Field(
        default=False,
        description="是否在 prompt 中注入近期详细记忆条目；默认只注入压缩层摘要",
    )
    target_prompt_names: list[str] = Field(
        default_factory=lambda: ["default_chatter_user_prompt"],
        description="允许注入连续记忆的 prompt 模板名列表",
    )
    recent_entry_limit: int = Field(
        default=5,
        description="注入 prompt 时展示的近期详细记忆条数",
    )
    summary_limit_per_level: int = Field(
        default=3,
        description="注入 prompt 时每层展示的摘要条数上限",
    )
    compression_model_task: str = Field(
        default="",
        description="压缩连续记忆使用的任务模型名称，留空则复用 model.task_name",
    )
    payload_history_trim_count_on_update: int = Field(
        default=20,
        description="每次连续记忆更新后，裁剪聊天流历史中最旧消息条数（0=不裁剪）",
    )


class DiaryConfig(BaseConfig):
    """日记插件配置。"""

    config_name = "config"
    config_description = "日记插件配置"

    plugin: PluginSection = Field(default_factory=PluginSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    format: FormatSection = Field(default_factory=FormatSection)
    dedup: DedupSection = Field(default_factory=DedupSection)
    reminder: ReminderSection = Field(default_factory=ReminderSection)
    auto_diary: AutoDiarySection = Field(default_factory=AutoDiarySection)
    model: ModelSection = Field(default_factory=ModelSection)
    continuous_memory: ContinuousMemorySection = Field(
        default_factory=ContinuousMemorySection
    )
