"""Kernel 启动引导。

在系统启动时调用 `bootstrap()` 完成：
1. 加载统一配置
2. 初始化核心服务（EventBus, LogStore, Scheduler, TaskManager）
3. 注册到 DI 容器

用法：
    from src.kernel.bootstrap import bootstrap, shutdown

    await bootstrap()
    # ... 系统运行 ...
    await shutdown()
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .container import container
from .config.unified import init_config, get_config
from .protocols import (
    EventBusProtocol,
    LogStoreProtocol,
    SchedulerProtocol,
)


_bootstrapped = False


async def bootstrap(
    config_path: str | Path = "config/elysium.toml",
    *,
    register_services: bool = True,
) -> None:
    """引导 kernel 层。

    Args:
        config_path: 配置文件路径
        register_services: 是否自动注册默认服务实现
    """
    global _bootstrapped
    if _bootstrapped:
        return

    # 1. 配置
    cfg = init_config(config_path)

    # 2. 注册默认服务实现
    if register_services:
        _register_event_bus()
        _register_log_store(cfg.runtime.db_path)
        _register_scheduler()
        _register_task_manager(cfg.runtime.process_workers)

    _bootstrapped = True


def _register_event_bus() -> None:
    """注册事件总线。"""
    from .event import get_event_bus

    bus = get_event_bus()
    container.register(EventBusProtocol, bus)


def _register_log_store(db_path: str) -> None:
    """注册日志存储。"""
    try:
        from .logger.logger import _global_log_store

        if _global_log_store is not None:
            container.register(LogStoreProtocol, _global_log_store)
    except ImportError:
        pass


def _register_scheduler() -> None:
    """注册调度器。"""
    try:
        from .scheduler import get_scheduler

        scheduler = get_scheduler()
        container.register(SchedulerProtocol, scheduler)
    except (ImportError, Exception):
        pass


def _register_task_manager(process_workers: int) -> None:
    """注册任务管理器（不通过 Protocol，直接类型注册）。"""
    from .concurrency import get_task_manager

    tm = get_task_manager()
    container.register(type(tm), tm)


async def shutdown() -> None:
    """优雅关闭 kernel 层。"""
    global _bootstrapped

    # 关闭日志存储
    try:
        from .logger.logger import _close_global_log_store

        _close_global_log_store()
    except (ImportError, Exception):
        pass

    # 关闭调度器
    try:
        from .scheduler import get_scheduler

        scheduler = get_scheduler()
        if hasattr(scheduler, "shutdown"):
            scheduler.shutdown()
    except (ImportError, Exception):
        pass

    container.reset()
    _bootstrapped = False
