"""
Logger 模块

基于 rich 库的统一日志系统，支持彩色渲染、元数据跟踪、SQLite 结构化存储和异常格式化。

用法示例:
    from src.kernel.logger import (
        initialize_logger_system, get_logger, COLOR, LOG_OUTPUT_EVENT, query_logs
    )

    # 初始化全局日志配置（在核心启动时调用）
    initialize_logger_system(log_level="INFO", db_path="data/logs.db")

    # 创建日志记录器（将使用全局配置）
    logger = get_logger("my_logger", display="我的日志", color=COLOR.BLUE)
    logger.info("Hello World!")

    # 查询日志
    results = query_logs(level="ERROR", search="崩溃", limit=20)
"""

from .logger import (
    Logger,
    initialize_logger_system,
    get_global_log_config,
    get_logger,
    remove_logger,
    get_all_loggers,
    clear_all_loggers,
    shutdown_logger_system,
    shutdown_logger_system_async,
    install_rich_traceback_formatter,
    LOG_OUTPUT_EVENT,
)
from .color import COLOR, get_rich_color, DEFAULT_LEVEL_COLORS
from .db_store import LogStore, SESSION_ID
from .stdlib_bridge import install_stdlib_bridge, uninstall_stdlib_bridge


def query_logs(
    level: str | None = None,
    module: str | None = None,
    search: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """查询日志（供 webui/CLI 使用）。

    Args:
        level: 过滤级别（如 "ERROR"）
        module: 过滤模块（支持前缀匹配）
        search: 全文搜索关键词（FTS5）
        since: 起始时间（ISO 格式）
        until: 截止时间（ISO 格式）
        limit: 返回条数上限
        offset: 偏移量

    Returns:
        日志记录列表
    """
    from .logger import _global_log_store
    if _global_log_store is None:
        return []
    return _global_log_store.query(
        level=level, module=module, search=search,
        since=since, until=until, limit=limit, offset=offset,
    )


__all__ = [
    # 全局初始化
    "initialize_logger_system",
    "get_global_log_config",
    "shutdown_logger_system",
    "shutdown_logger_system_async",
    # 主要接口
    "get_logger",
    "Logger",
    "COLOR",
    # 查询接口
    "query_logs",
    # 存储引擎
    "LogStore",
    "SESSION_ID",
    # stdlib 桥接
    "install_stdlib_bridge",
    "uninstall_stdlib_bridge",
    # 辅助函数
    "remove_logger",
    "get_all_loggers",
    "clear_all_loggers",
    "get_rich_color",
    "install_rich_traceback_formatter",
    "DEFAULT_LEVEL_COLORS",
    # 事件广播相关
    "LOG_OUTPUT_EVENT",
]

# 版本信息
__version__ = "2.0.0"
