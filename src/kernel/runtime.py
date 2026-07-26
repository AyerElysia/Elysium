"""异步运行时生命周期管理。

提供结构化的系统生命周期：startup → running → shutdown。
基于 Python 3.12 的 asyncio.TaskGroup 实现 structured concurrency。

用法：
    from src.kernel.runtime import Runtime

    runtime = Runtime()

    @runtime.on_startup
    async def init_services():
        ...

    @runtime.on_shutdown
    async def cleanup():
        ...

    # 启动（阻塞直到收到停止信号）
    await runtime.run()
"""

from __future__ import annotations

import asyncio
import signal
import time
from enum import Enum
from typing import Any, Callable, Coroutine

from .container import container
from .config.unified import get_config


class RuntimeState(str, Enum):
    """运行时状态。"""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


LifecycleHook = Callable[[], Coroutine[Any, Any, None]]


class Runtime:
    """Elysium 异步运行时。

    管理系统完整生命周期，提供：
    - 结构化启动/关闭钩子
    - 优雅停止（SIGINT/SIGTERM）
    - 后台任务组（自动取消）
    - 健康检查心跳
    """

    def __init__(self) -> None:
        self._state = RuntimeState.IDLE
        self._startup_hooks: list[LifecycleHook] = []
        self._shutdown_hooks: list[LifecycleHook] = []
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._stop_event = asyncio.Event()
        self._start_time: float = 0.0

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def uptime(self) -> float:
        """运行时长（秒）。"""
        if self._start_time == 0:
            return 0.0
        return time.monotonic() - self._start_time

    # ─── 生命周期钩子 ─────────────────────────

    def on_startup(self, fn: LifecycleHook) -> LifecycleHook:
        """注册启动钩子（装饰器）。"""
        self._startup_hooks.append(fn)
        return fn

    def on_shutdown(self, fn: LifecycleHook) -> LifecycleHook:
        """注册关闭钩子（装饰器）。"""
        self._shutdown_hooks.append(fn)
        return fn

    # ─── 后台任务 ─────────────────────────────

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """创建受管后台任务。

        任务会在 runtime 停止时自动取消。
        """
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ─── 主循环 ───────────────────────────────

    async def run(self) -> None:
        """启动运行时，阻塞直到停止信号。"""
        await self.start()
        await self._stop_event.wait()
        await self.stop()

    async def start(self) -> None:
        """执行启动流程。"""
        if self._state != RuntimeState.IDLE:
            return

        self._state = RuntimeState.STARTING
        self._start_time = time.monotonic()

        # 执行启动钩子
        for hook in self._startup_hooks:
            await hook()

        # 注册信号处理
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._signal_stop)
            except (NotImplementedError, RuntimeError):
                pass  # Windows 或非主线程

        self._state = RuntimeState.RUNNING

    async def stop(self) -> None:
        """执行优雅关闭。"""
        if self._state not in (RuntimeState.RUNNING, RuntimeState.STARTING):
            return

        self._state = RuntimeState.STOPPING
        cfg = get_config()
        timeout = cfg.runtime.shutdown_timeout

        # 取消后台任务
        if self._background_tasks:
            for task in self._background_tasks:
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # 执行关闭钩子（带超时）
        try:
            async with asyncio.timeout(timeout):
                for hook in reversed(self._shutdown_hooks):
                    await hook()
        except TimeoutError:
            pass  # 超时则强制继续

        self._state = RuntimeState.STOPPED

    def request_stop(self) -> None:
        """请求停止（可从任何地方调用）。"""
        self._stop_event.set()

    def _signal_stop(self) -> None:
        """信号处理回调。"""
        self.request_stop()


# ─────────────────────────────────────────────
# 全局运行时实例
# ─────────────────────────────────────────────

_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """获取全局运行时实例。"""
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime
