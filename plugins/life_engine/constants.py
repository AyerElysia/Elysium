"""life_engine 常量定义。

所有魔法数字和硬编码字符串应在此集中管理。
"""

from __future__ import annotations



# ============================================================================
# 时间相关常量
# ============================================================================

# 外部消息活跃时间窗口（分钟）
EXTERNAL_MESSAGE_ACTIVE_WINDOW_MINUTES: int = 5

# 心跳空闲警告阈值
HEARTBEAT_IDLE_WARNING_THRESHOLD: int = 5
HEARTBEAT_IDLE_CRITICAL_THRESHOLD: int = 12

# 记忆衰减检查间隔（小时）
MEMORY_DECAY_CHECK_INTERVAL_HOURS: int = 24


# ============================================================================
# TODO 优先级常量
# ============================================================================

# TODO 截止时间优先级
TODO_NO_DEADLINE_PRIORITY: int = 9999
TODO_OVERDUE_BASE_PRIORITY: int = -1000
TODO_URGENT_DAYS_THRESHOLD: int = 3


# ============================================================================
# 梦境系统常量
# ============================================================================

# 梦境种子生成
DREAM_SEED_MIN_EVENTS: int = 3
DREAM_SEED_FALLBACK_AROUSAL: float = 0.25
DREAM_SEED_FALLBACK_IMPORTANCE: float = 0.3
DREAM_SEED_FALLBACK_DREAMABILITY: float = 0.4

# REM 阶段参数
REM_MAX_DEPTH_BASE: int = 3
REM_DECAY_FACTOR_BASE: float = 0.7
REM_SEEDS_PER_ROUND_BASE: int = 2
REM_WALK_ROUNDS_BASE: int = 2

# 梦境渐进参数
DREAM_DEPTH_INCREMENT_PER_CYCLE: int = 1
DREAM_DECAY_INCREMENT_PER_CYCLE: float = 0.05
DREAM_MAX_DECAY_FACTOR: float = 0.85


# ============================================================================
# 文件系统常量
# ============================================================================

# 文件大小限制（字节）
MAX_FILE_READ_SIZE: int = 10 * 1024 * 1024  # 10MB
MAX_FILE_WRITE_SIZE: int = 5 * 1024 * 1024  # 5MB

# 文件列表限制
MAX_FILE_LIST_DEPTH: int = 3
MAX_FILE_LIST_ITEMS: int = 1000


# ============================================================================
# 网络请求常量
# ============================================================================

# Tavily API
DEFAULT_TAVILY_BASE_URL: str = "https://api.tavily.com"
TAVILY_REQUEST_TIMEOUT_SECONDS: int = 30
TAVILY_MAX_RESULTS: int = 5

# HTTP 请求
HTTP_REQUEST_TIMEOUT_SECONDS: int = 30
HTTP_MAX_RETRIES: int = 3
HTTP_RETRY_BACKOFF_FACTOR: float = 2.0


# ============================================================================
# 记忆算法常量
# ============================================================================

# RRF 融合参数
RRF_K: int = 60

# 激活扩散参数
SPREAD_DECAY: float = 0.7
SPREAD_THRESHOLD: float = 0.3

# 遗忘衰减参数
DECAY_LAMBDA: float = 0.05
PRUNE_THRESHOLD: float = 0.1

# 梦境学习参数
DREAM_LEARNING_RATE: float = 0.05


# ============================================================================
# 文本处理常量
# ============================================================================

# 文本截断
TEXT_TRUNCATE_SUFFIX: str = "..."
TEXT_SHORT_DISPLAY_LENGTH: int = 100
TEXT_MEDIUM_DISPLAY_LENGTH: int = 500

# MEMORY.md 管理（软提醒阈值；写入本身不因这些值被拒绝）
MEMORY_PROMPT_SOFT_LIMIT_BYTES: int = 24 * 1024
MEMORY_WRITE_WARNING_THRESHOLD_BYTES: int = 32 * 1024
MEMORY_MAX_DURABLE_ITEMS: int = 80
MEMORY_MAX_FADING_ITEMS: int = 8
MEMORY_CHATTER_MAX_DURABLE_ITEMS: int = 32
MEMORY_CHATTER_MAX_ACTIVE_ITEMS: int = 10
MEMORY_MAINTENANCE_INTERVAL_HOURS: int = 24

# 主动唤醒理由验证
PROACTIVE_WAKE_MIN_REASON_CHARS: int = 28
PROACTIVE_WAKE_MIN_SEGMENTS: int = 2
PROACTIVE_WAKE_REQUIRED_IMPORTANCE: set[str] = {"high", "critical"}
PROACTIVE_WAKE_KEYWORDS: tuple[str, ...] = (
    "信息差",
    "影响",
    "风险",
    "必要",
    "依据",
    "观察",
    "后果",
    "时效",
    "上下文",
    "目标",
)


# ============================================================================
# life_chatter 主意识常量
# ============================================================================

# 统一 life_chatter 主意识读取 life runtime 的全局 cursor key。
# 旧的 per-stream cursor 仍保留用于兼容，但主链路使用这个 key。
LIFE_CHATTER_GLOBAL_CURSOR_KEY: str = "__life_chatter_global__"
