"""
统一日志系统

基于 rich 库的日志输出，支持彩色渲染、元数据跟踪和 SQLite 结构化存储。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.traceback import install as install_rich_traceback

from .color import COLOR, get_rich_color
from .db_store import LogStore

if TYPE_CHECKING:
    from src.kernel.event import EventBus


@lru_cache(maxsize=1)
def _get_event_bus() -> EventBus:
    """获取全局事件总线实例（使用 lru_cache 实现单例）。"""
    from src.kernel.event import get_event_bus
    return get_event_bus()


# 日志广播事件名称
LOG_OUTPUT_EVENT = "log_output"

_event_broadcast_tasks: set[asyncio.Task[Any]] = set()
_event_broadcast_lock = threading.Lock()
_event_broadcast_stopped = False

# get_logger 默认颜色池（仅当未显式传 color 时使用）
# 使用 16 个十六进制颜色，避免与 COLOR 枚举中的命名颜色重复。
_DEFAULT_NAME_COLOR_PALETTE: tuple[str, ...] = (
    "#5E81AC",
    "#88C0D0",
    "#81A1C1",
    "#8FBCBB",
    "#A3BE8C",
    "#EBCB8B",
    "#D08770",
    "#BF616A",
    "#B48EAD",
    "#7AA2F7",
    "#9ECE6A",
    "#E0AF68",
    "#F7768E",
    "#7DCFFF",
    "#C0CAF5",
    "#BB9AF7",
)


def _set_event_broadcast_stopped(value: bool) -> None:
    global _event_broadcast_stopped
    with _event_broadcast_lock:
        _event_broadcast_stopped = value


def _discard_event_broadcast_task(task: asyncio.Task[Any]) -> None:
    with _event_broadcast_lock:
        _event_broadcast_tasks.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


def _schedule_event_broadcast(event_bus: EventBus, log_data: dict[str, Any]) -> None:
    with _event_broadcast_lock:
        if _event_broadcast_stopped:
            return

    task = asyncio.create_task(
        event_bus.publish(LOG_OUTPUT_EVENT, log_data),
        name="log_event_broadcast",
    )

    with _event_broadcast_lock:
        if _event_broadcast_stopped:
            task.cancel()
        else:
            _event_broadcast_tasks.add(task)
    task.add_done_callback(_discard_event_broadcast_task)
    if task.done():
        _discard_event_broadcast_task(task)


def _maybe_resume_event_broadcast() -> None:
    with _config_lock:
        enabled = bool(_global_config.get("enable_event_broadcast", True))
    if enabled:
        _set_event_broadcast_stopped(False)


async def _drain_event_broadcast_tasks(timeout: float = 1.0) -> None:
    with _event_broadcast_lock:
        tasks = [task for task in _event_broadcast_tasks if not task.done()]

    if not tasks:
        return

    done, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))
    for task in done:
        _discard_event_broadcast_task(task)

    if not pending:
        return

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def _get_default_logger_color_by_name(name: str) -> str:
    """根据 logger 名称稳定映射默认颜色。"""
    normalized_name = (name or "").strip().lower() or "default"
    digest = hashlib.md5(normalized_name.encode("utf-8")).digest()
    color_index = digest[0] % len(_DEFAULT_NAME_COLOR_PALETTE)
    return _DEFAULT_NAME_COLOR_PALETTE[color_index]


def _strip_rich_markup(message: str) -> str:
    """移除 Rich markup 标签，返回纯文本。

    用于所有非控制台 sink（SQLite 与按日期滚动的文件镜像），避免把控制台
    样式标签写进持久记录。控制台仍然渲染 markup。

    Args:
        message: 可能包含 Rich markup 的日志消息

    Returns:
        str: 去除 markup 后的纯文本消息
    """
    try:
        return Text.from_markup(message).plain
    except Exception:
        return message

# 全局配置
_global_config: dict[str, Any] = {
    # 空字符串表示不写文件镜像；由 initialize_logger_system(log_dir=...) 设置。
    "log_dir": "",
    "log_level": "DEBUG",  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    "enable_db": False,
    "db_path": "data/logs.db",
    "retention_debug_days": 3,
    "retention_info_days": 30,
    "enable_event_broadcast": True,
}
_config_lock = threading.Lock()

# 全局共享的日志存储（所有logger共享同一个）
_global_log_store: LogStore | None = None
_stdlib_bridge_handler: logging.Handler | None = None
_log_store_lock = threading.Lock()

# 日志等级优先级映射
_LOG_LEVEL_PRIORITY = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "ERROR": 3,
    "CRITICAL": 4,
}


class Logger:
    """日志记录器

    提供彩色日志输出、元数据跟踪、rich 渲染支持和 SQLite 结构化存储。

    Attributes:
        name: 日志记录器名称
        display: 显示名称（用于输出前缀）
        color: 日志颜色
        console: rich.Console 实例
        metadata: 元数据字典
        _lock: 线程锁
        _enable_db: 是否启用数据库存储
        _log_level: 日志等级
    """

    def __init__(
        self,
        name: str,
        display: str | None = None,
        color: COLOR | str = COLOR.WHITE,
        console: Console | None = None,
        enable_db: bool | None = None,
        enable_event_broadcast: bool = True,
        log_level: str | None = None,
    ) -> None:
        """初始化日志记录器

        Args:
            name: 日志记录器名称（唯一标识）
            display: 显示名称，如果为 None 则使用 name
            color: 日志颜色
            console: rich.Console 实例，如果为 None 则创建默认实例
            enable_db: 是否启用数据库存储（使用全局共享的 LogStore）；
                None 表示跟随全局配置，且在 initialize_logger_system 之后生效
            enable_event_broadcast: 是否启用事件广播（发布到 on_log_output 事件）
            log_level: 日志等级，如果为 None 则使用全局配置
        """
        self.name = name
        self.display = display or name
        self.color = get_rich_color(color)
        self.metadata: dict[str, Any] = {}
        self._lock = threading.Lock()
        # enable_db 与 log_level 同理：为 None 时动态跟随全局配置。
        # 否则在 import 期（initialize_logger_system 之前）创建的 logger
        # 会把 enable_db=False 永久冻结下来，导致整棵 kernel.llm.* 日志
        # 从不落库——失败与重试链路在 logs.db 里完全不可见。
        self._use_global_enable_db: bool = enable_db is None
        self._enable_db = bool(enable_db)
        self._enable_event_broadcast = enable_event_broadcast

        # 设置日志等级
        # _use_global_level=True 时，_should_log 动态读取 _global_config，
        # 确保 initialize_logger_system 调整级别后已创建的 logger 也能响应。
        self._use_global_level: bool = log_level is None
        with _config_lock:
            self._log_level = (log_level or _global_config["log_level"]).upper()

        # 创建或使用提供的 Console
        if console is None:
            self.console = Console(
                stderr=True,
                highlight=False,
                force_terminal=True,
                legacy_windows=False,
            )
        else:
            self.console = console

    def debug(self, message: str, **kwargs: Any) -> None:
        """输出 DEBUG 级别日志

        Args:
            message: 日志消息
            **kwargs: 额外的元数据
        """
        self._log("DEBUG", message, COLOR.DEBUG, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """输出 INFO 级别日志

        Args:
            message: 日志消息
            **kwargs: 额外的元数据
        """
        self._log("INFO", message, COLOR.INFO, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """输出 WARNING 级别日志

        Args:
            message: 日志消息
            **kwargs: 额外的元数据
        """
        self._log("WARNING", message, COLOR.WARNING, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """输出 ERROR 级别日志

        Args:
            message: 日志消息
            **kwargs: 额外的元数据
        """
        self._log("ERROR", message, COLOR.ERROR, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        """输出 CRITICAL 级别日志

        Args:
            message: 日志消息
            **kwargs: 额外的元数据
        """
        self._log("CRITICAL", message, COLOR.CRITICAL, **kwargs)

    def _log(
        self,
        level: str,
        message: str,
        color: COLOR | str,
        **metadata: Any,
    ) -> None:
        """内部日志输出方法

        Args:
            level: 日志级别
            message: 日志消息
            color: 日志颜色
            **metadata: 额外的元数据
        """
        should_output = self._should_log(level)
        if not should_output:
            return

        with self._lock:
            # 合并元数据
            all_metadata = {**self.metadata, **metadata}
            exc_info = all_metadata.pop("exc_info", None)

            # 构建时间戳
            now = datetime.now()
            timestamp_short = now.strftime("%H:%M:%S")
            timestamp_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            level_color = get_rich_color(color)
            exc_lines: list[str] = []

            if exc_info:
                import traceback

                if exc_info is True:
                    exc_type, exc_val, exc_tb = sys.exc_info()
                    exc_lines = traceback.format_exception(exc_type, exc_val, exc_tb)
                elif isinstance(exc_info, BaseException):
                    exc_lines = traceback.format_exception(
                        type(exc_info),
                        exc_info,
                        exc_info.__traceback__,
                    )
                else:
                    exc_lines = [str(exc_info)]

            # 输出到控制台（按日志级别过滤）
            if should_output:
                # 使用 rich.Text 构建彩色输出
                text = Text()
                text.append(f"[{timestamp_short}] ", style="dim")
                text.append(f"{self.display}", style=self.color)
                text.append(" | ", style="dim")
                text.append(f"{level}", style=level_color)
                text.append(" | ", style="dim")
                try:
                    text.append(Text.from_markup(message))
                except Exception:
                    # 如果 markup 解析失败（例如含有未闭合并非意图作为 markup 的方括号），回退到普通文本
                    text.append(message)

                self.console.print(text)

                # 如果有元数据，显示在下方
                if all_metadata:
                    metadata_str = " | ".join([f"{k}={v}" for k, v in all_metadata.items()])
                    metadata_text = Text(metadata_str, style="dim")
                    self.console.print(metadata_text)

                if exc_lines:
                    exc_text = Text("".join(exc_lines), style="dim")
                    self.console.print(exc_text)

            # 输出到数据库（如果启用，不受日志级别过滤）
            if self._db_enabled():
                if _global_log_store is not None:
                    plain_message = _strip_rich_markup(message)
                    meta = dict(all_metadata)
                    if exc_lines:
                        meta["exception"] = "".join(exc_lines)
                    _global_log_store.write(
                        level=level,
                        module=self.name,
                        message=plain_message,
                        metadata=meta or None,
                    )

            # 发布事件广播（如果启用）
            if self._enable_event_broadcast:
                self._emit_log_event(timestamp_iso, level, message, all_metadata)

    def _emit_log_event(
        self,
        timestamp: str,
        level: str,
        message: str,
        metadata: dict[str, Any],
    ) -> None:
        """发布日志事件到事件总线。

        Args:
            timestamp: ISO 格式时间戳
            level: 日志级别
            message: 日志消息
            metadata: 元数据字典
        """
        try:
            # 构建事件数据
            log_data: dict[str, Any] = {
                "timestamp": timestamp,
                "level": level,
                "logger_name": self.name,
                "display": self.display,
                "color": self.color,
                "message": message,
            }

            # 添加元数据（如果有）
            if metadata:
                log_data["metadata"] = dict(metadata)

            # 获取事件总线
            event_bus = _get_event_bus()

            # 尝试发布事件（即发即弃）
            try:
                asyncio.get_running_loop()
                _schedule_event_broadcast(event_bus, log_data)
            except RuntimeError:
                # 没有运行中的事件循环
                # 事件广播是可选功能，静默忽略
                pass

        except Exception:
            # 事件广播失败不应影响日志系统本身
            # 静默忽略错误
            pass

    def _should_log(self, level: str) -> bool:
        """检查是否应该输出该级别的日志

        Args:
            level: 日志级别

        Returns:
            bool: 是否应该输出
        """
        if self._use_global_level:
            with _config_lock:
                current_level = _global_config["log_level"]
        else:
            current_level = self._log_level
        level_priority = _LOG_LEVEL_PRIORITY.get(level.upper(), 0)
        current_priority = _LOG_LEVEL_PRIORITY.get(current_level, 0)
        return level_priority >= current_priority

    def _db_enabled(self) -> bool:
        """检查是否应该写入数据库。

        与 ``_should_log`` 同构：未显式指定 enable_db 时动态读取全局配置，
        这样在 ``initialize_logger_system`` 之前创建的 logger 也能在初始化
        之后正常落库。

        Returns:
            bool: 是否写入 SQLite 存储
        """
        if self._use_global_enable_db:
            with _config_lock:
                return bool(_global_config["enable_db"])
        return self._enable_db

    def set_log_level(self, level: str) -> None:
        """设置日志等级

        显式调用后不再跟随全局配置变更。

        Args:
            level: 日志等级（DEBUG, INFO, WARNING, ERROR, CRITICAL）
        """
        with self._lock:
            self._log_level = level.upper()
            self._use_global_level = False

    def get_log_level(self) -> str:
        """获取当前日志等级

        Returns:
            str: 当前日志等级
        """
        return self._log_level

    def set_metadata(self, key: str, value: Any) -> None:
        """设置元数据

        Args:
            key: 元数据键
            value: 元数据值
        """
        with self._lock:
            self.metadata[key] = value

    def get_metadata(self, key: str) -> Any:
        """获取元数据

        Args:
            key: 元数据键

        Returns:
            元数据值，如果不存在则返回 None
        """
        return self.metadata.get(key)

    def clear_metadata(self) -> None:
        """清除所有元数据"""
        with self._lock:
            self.metadata.clear()

    def remove_metadata(self, key: str) -> None:
        """移除指定的元数据

        Args:
            key: 元数据键
        """
        with self._lock:
            self.metadata.pop(key, None)

    def print_panel(
        self,
        message: str,
        title: str | None = None,
        border_style: str | None = None,
    ) -> None:
        """输出面板格式的日志

        Args:
            message: 日志消息
            title: 面板标题
            border_style: 边框样式
        """
        with self._lock:
            if border_style is None:
                border_style = self.color

            panel = Panel(
                message,
                title=title or self.display,
                border_style=border_style,
            )
            self.console.print(panel)

    def print_rich(self, *args: Any, **kwargs: Any) -> None:
        """直接使用 rich 打印

        Args:
            *args: 传递给 console.print 的参数
            **kwargs: 传递给 console.print 的关键字参数
        """
        with self._lock:
            self.console.print(*args, **kwargs)

    def __repr__(self) -> str:
        """日志记录器字符串表示"""
        db_status = "enabled" if self._db_enabled() else "disabled"
        return (
            f"Logger(name='{self.name}', display='{self.display}', "
            f"color='{self.color}', db={db_status})"
        )


# 全局 logger 注册表
_loggers: dict[str, Logger] = {}
_lock = threading.Lock()


def initialize_logger_system(
    log_level: str = "DEBUG",
    enable_db: bool = True,
    db_path: str | Path = "data/logs.db",
    retention_debug_days: int = 3,
    retention_info_days: int = 30,
    enable_event_broadcast: bool = True,
    log_dir: str | Path | None = None,
    **_legacy: Any,
) -> None:
    """初始化日志系统全局配置

    此方法应在核心启动时调用，用于设置全局的日志配置。
    之后创建的所有 logger 将默认使用这些配置（除非在创建时显式指定）。

    所有 logger 共享同一个 SQLite 日志存储（data/logs.db）。

    Args:
        log_level: 全局日志等级（DEBUG, INFO, WARNING, ERROR, CRITICAL）
        enable_db: 是否启用 SQLite 结构化存储
        db_path: SQLite 数据库文件路径
        retention_debug_days: DEBUG 级别日志保留天数
        retention_info_days: INFO+ 级别日志保留天数
        enable_event_broadcast: 是否默认启用事件广播
        log_dir: 按日期滚动的纯文本镜像目录；``None`` 表示不写文件镜像。
            调用方必须显式传入，测试因此不会意外在仓库里落下日志文件。

    Example:
        >>> from src.kernel.logger import initialize_logger_system
        >>> initialize_logger_system(log_level="INFO", db_path="data/logs.db")
    """
    global _global_log_store, _stdlib_bridge_handler
    _set_event_broadcast_stopped(False)

    with _config_lock:
        _global_config["log_level"] = log_level.upper()
        _global_config["enable_db"] = enable_db
        _global_config["db_path"] = str(db_path)
        _global_config["retention_debug_days"] = retention_debug_days
        _global_config["retention_info_days"] = retention_info_days
        _global_config["enable_event_broadcast"] = enable_event_broadcast
        # 以前这个键只是被声明、从来没有人读；现在它是文件镜像的唯一来源。
        _global_config["log_dir"] = str(log_dir) if log_dir is not None else ""

    # 创建或重新创建全局 LogStore，并确保 stdlib bridge 只有一个。
    from .stdlib_bridge import install_stdlib_bridge, uninstall_stdlib_bridge

    if _stdlib_bridge_handler is not None:
        uninstall_stdlib_bridge(_stdlib_bridge_handler)
        _stdlib_bridge_handler = None

    with _log_store_lock:
        if _global_log_store is not None:
            _global_log_store.close()
            _global_log_store = None
        if enable_db:
            _global_log_store = LogStore(
                db_path=db_path,
                retention_debug_days=retention_debug_days,
                retention_info_days=retention_info_days,
                log_dir=log_dir,
            )

    if _global_log_store is not None:
        bridge_level = getattr(logging, log_level.upper(), logging.INFO)
        _stdlib_bridge_handler = install_stdlib_bridge(
            _global_log_store,
            level=bridge_level,
        )

    # 安装 rich traceback
    install_rich_traceback_formatter()

def get_global_log_config() -> dict[str, Any]:
    """获取全局日志配置

    Returns:
        dict[str, Any]: 全局日志配置字典
    """
    with _config_lock:
        return dict(_global_config)


def get_logger(
    name: str,
    display: str | None = None,
    color: COLOR | str | None = None,
    console: Console | None = None,
    enable_db: bool | None = None,
    enable_event_broadcast: bool | None = None,
    log_level: str | None = None,
    **_legacy: Any,
) -> Logger:
    """获取或创建日志记录器

    所有 logger 共享同一个 SQLite 日志存储，配置通过 initialize_logger_system() 设置。

    Args:
        name: 日志记录器名称（唯一标识）
        display: 显示名称，如果为 None 则使用 name
        color: 日志颜色；为 None 时根据 name 自动映射默认颜色
        console: rich.Console 实例
        enable_db: 是否启用 SQLite 存储（None 则使用全局配置）
        enable_event_broadcast: 是否启用事件广播（None 则使用全局配置）
        log_level: 日志等级（None 则使用全局配置）

    Returns:
        Logger: 日志记录器实例

    Example:
        >>> from src.kernel.logger import get_logger, COLOR, initialize_logger_system
        >>> initialize_logger_system(log_level="INFO")
        >>> logger = get_logger("my_logger", display="我的日志", color=COLOR.BLUE)
        >>> logger.info("Hello World!")
    """
    with _lock:
        if name not in _loggers:
            _maybe_resume_event_broadcast()
            # 使用全局配置作为默认值。
            # 注意 enable_db 不在此处解析：import 期创建的 logger 会把当时的
            # False 永久冻结，导致 initialize_logger_system 之后仍不落库。
            # 保持 None 传入，由 Logger._db_enabled() 动态读取全局配置。
            with _config_lock:
                actual_enable_event_broadcast = (
                    enable_event_broadcast if enable_event_broadcast is not None
                    else _global_config["enable_event_broadcast"]
                )
            actual_color = color if color is not None else _get_default_logger_color_by_name(name)

            _loggers[name] = Logger(
                name=name,
                display=display,
                color=actual_color,
                console=console,
                enable_db=enable_db,
                enable_event_broadcast=actual_enable_event_broadcast,
                log_level=log_level,
            )
        return _loggers[name]


def remove_logger(name: str) -> None:
    """移除日志记录器

    Args:
        name: 日志记录器名称
    """
    with _lock:
        _loggers.pop(name, None)


def clear_all_loggers() -> None:
    """清除所有日志记录器"""
    with _lock:
        _loggers.clear()


def get_all_loggers() -> dict[str, Logger]:
    """获取所有日志记录器

    Returns:
        dict[str, Logger]: 所有日志记录器的字典
    """
    with _lock:
        return dict(_loggers)

def _close_global_log_store() -> None:
    """关闭全局 LogStore 并卸载 stdlib bridge。"""
    global _global_log_store, _stdlib_bridge_handler
    if _stdlib_bridge_handler is not None:
        from .stdlib_bridge import uninstall_stdlib_bridge

        uninstall_stdlib_bridge(_stdlib_bridge_handler)
        _stdlib_bridge_handler = None
    with _log_store_lock:
        if _global_log_store is not None:
            _global_log_store.close()
            _global_log_store = None


async def shutdown_logger_system_async(timeout: float = 1.0) -> None:
    """异步关闭日志系统，先收尾日志事件广播任务。"""
    _set_event_broadcast_stopped(True)
    await _drain_event_broadcast_tasks(timeout=timeout)
    _close_global_log_store()


def shutdown_logger_system() -> None:
    """关闭日志系统，释放所有资源。

    在异步运行时中优先使用 shutdown_logger_system_async()，这样可以等待
    已经创建的日志事件广播任务结束，避免事件循环关闭时报 pending task。
    """
    _set_event_broadcast_stopped(True)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        with _event_broadcast_lock:
            tasks = [task for task in _event_broadcast_tasks if not task.done()]
        for task in tasks:
            task.cancel()
    _close_global_log_store()


def install_rich_traceback_formatter():
    """安装 rich 的异常格式化

    使用 rich 格式化 Python 异常的回溯信息。
    """
    install_rich_traceback(
        console=Console(stderr=True),
        width=None,
        word_wrap=False,
        show_locals=False,
    )
