"""统一异常处理工具。

本模块提供 life_engine 插件的异常处理基础设施，包括：
- 自定义异常类层次结构
- 日志记录装饰器
- 安全任务取消
- 指数退避重试机制
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, ParamSpec, TypeVar

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("life_engine.error_handling")

P = ParamSpec("P")
T = TypeVar("T")


# ============================================================================
# 异常类层次结构
# ============================================================================


class LifeEngineError(Exception):
    """life_engine 基础异常类。"""

    pass


class TaskCancellationError(LifeEngineError):
    """任务取消异常。"""

    pass


class URLValidationError(LifeEngineError):
    """URL 验证异常。"""

    pass


class SerializationError(LifeEngineError):
    """序列化异常。"""

    pass


class RetryExhaustedError(LifeEngineError):
    """重试次数耗尽异常。"""

    pass


# ============================================================================
# 装饰器和工具函数
# ============================================================================


def log_and_suppress(
    exc_type: type[Exception] = Exception,
    default_return: Any = None,
    log_level: str = "warning",
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """装饰器：记录异常并返回默认值。

    用于替代 `except Exception: pass` 模式，确保异常被记录。

    Args:
        exc_type: 要捕获的异常类型
        default_return: 异常时返回的默认值
        log_level: 日志级别 (debug/info/warning/error)

    Returns:
        装饰器函数

    Example:
        @log_and_suppress(ValueError, default_return=0, log_level="warning")
        def parse_int(s: str) -> int:
            return int(s)
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except exc_type as e:
                log_func = getattr(logger, log_level, logger.warning)
                log_func(
                    f"{func.__name__} failed: {e.__class__.__name__}: {e}",
                    exc_info=log_level == "error",
                )
                return default_return

        return wrapper

    return decorator


def safe_cancel_task(task_id: str, task_manager: Any) -> bool:
    """安全取消任务，记录失败情况。

    Args:
        task_id: 任务 ID
        task_manager: TaskManager 实例

    Returns:
        是否成功取消
    """
    if not task_id:
        return False

    try:
        task_manager.cancel_task(task_id)
        logger.debug(f"Successfully cancelled task: {task_id}")
        return True
    except Exception as e:
        # 任务可能已完成或不存在
        logger.debug(f"Task cancellation note for {task_id}: {e}")
        return False


async def retry_with_backoff(
    func: Callable[..., T],
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """带指数退避的重试机制。

    Args:
        func: 要重试的异步函数
        max_retries: 最大重试次数
        initial_delay: 初始延迟（秒）
        backoff_factor: 退避因子
        exceptions: 要捕获的异常类型

    Returns:
        函数执行结果

    Raises:
        RetryExhaustedError: 所有重试都失败时
        最后一次尝试的异常: 如果不是指定的异常类型

    Example:
        async def fetch_data():
            return await api.get("/data")

        result = await retry_with_backoff(
            fetch_data,
            max_retries=3,
            exceptions=(asyncio.TimeoutError, ConnectionError)
        )
    """
    delay = initial_delay
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return await func()
        except exceptions as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries + 1} failed: {e}, "
                    f"retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                delay *= backoff_factor
            else:
                logger.error(
                    f"All {max_retries + 1} attempts failed for {func.__name__}"
                )

    raise RetryExhaustedError(
        f"Failed after {max_retries + 1} attempts: {last_exception}"
    ) from last_exception


