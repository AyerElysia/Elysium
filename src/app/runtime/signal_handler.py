"""信号处理器。

处理 SIGINT、SIGTERM 和 SIGHUP，以实现手动进程的优雅关闭。
"""

from __future__ import annotations

import asyncio
import signal
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bot import Bot


class SignalHandler:
    """信号处理器

    监听系统信号并协调 Bot 的优雅关闭。

    行为：
    - 第一次 SIGINT/SIGTERM：请求优雅关闭
    - 3 秒内第二次关闭信号：强制立即关闭
    - SIGHUP：请求优雅关闭，避免终端断开后遗留后台进程

    Attributes:
        bot: Bot 实例
        shutdown_requested: 关闭请求事件
        last_signal_time: 上次信号时间戳
        signal_count: 信号计数
    """

    def __init__(self, bot: "Bot") -> None:
        """初始化信号处理器

        Args:
            bot: Bot 实例
        """
        self.bot = bot
        self.shutdown_requested = asyncio.Event()
        self.last_signal_time = 0.0
        self.signal_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._original_handlers: dict[int, Any] = {}

    def register_signals(self) -> None:
        """注册 SIGINT、SIGTERM，并在支持的平台接管 SIGHUP。"""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # 注册 SIGINT (Ctrl+C)
        self._original_handlers[signal.SIGINT] = signal.signal(
            signal.SIGINT, self._handle_signal
        )

        # 注册 SIGTERM
        try:
            self._original_handlers[signal.SIGTERM] = signal.signal(
                signal.SIGTERM, self._handle_signal
            )
        except ValueError:
            # SIGTERM 在某些平台可能不可用
            pass

        # 注册 SIGHUP（终端关闭时优雅退出，避免残留后台进程）
        try:
            self._original_handlers[signal.SIGHUP] = signal.signal(
                signal.SIGHUP, self._handle_sighup
            )
        except (AttributeError, ValueError, OSError):
            # SIGHUP 在 Windows 不可用
            pass

    def _handle_sighup(self, signum: int, frame: Any) -> None:
        """Treat terminal detachment as a graceful shutdown request."""

        self._schedule_log(
            "info",
            "收到 SIGHUP（终端关闭），Elysium 将优雅退出",
        )
        self.shutdown_requested.set()
        self.bot._running = False

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """处理信号

        Args:
            signum: 信号编号
            frame: 当前堆栈帧
        """
        current_time = time.time()

        # 检查是否在 3 秒内多次触发
        if current_time - self.last_signal_time < 3.0:
            self.signal_count += 1
        else:
            self.signal_count = 1

        self.last_signal_time = current_time

        # 第一次信号：请求优雅关闭
        if self.signal_count == 1:
            self._schedule_log(
                "info",
                "已收到关闭信号。再次按下Ctrl+C强制退出...",
            )
            self.shutdown_requested.set()

            # 设置 _running 标志，让主循环自然退出
            self.bot._running = False

        # 第二次信号（3 秒内）：强制立即关闭
        elif self.signal_count >= 2:
            self._schedule_log("warning", "正在强制关闭...")
            # 强制退出（不执行清理）
            import sys

            sys.exit(1)

    def _schedule_log(self, level: str, message: str) -> None:
        """将信号日志投递回事件循环，避免在信号上下文中争用日志锁。"""
        logger = getattr(self.bot, "logger", None)
        log_method = getattr(logger, level, None)
        if log_method is None:
            return

        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(log_method, message)

    def restore_handlers(self) -> None:
        """恢复原始信号处理器"""
        for sig, handler in self._original_handlers.items():
            signal.signal(sig, handler)
        self._original_handlers.clear()

    async def wait_for_shutdown_signal(self) -> None:
        """等待关闭信号

        阻塞直到收到关闭信号。
        """
        await self.shutdown_requested.wait()

    def is_shutdown_requested(self) -> bool:
        """检查是否已请求关闭

        Returns:
            bool: 是否已请求关闭
        """
        return self.shutdown_requested.is_set()

    def reset(self) -> None:
        """重置信号处理器状态

        清除关闭请求和计数器。
        """
        self.shutdown_requested.clear()
        self.signal_count = 0
        self.last_signal_time = 0.0

    def __del__(self) -> None:
        """析构函数，恢复原始信号处理器"""
        self.restore_handlers()


__all__ = ["SignalHandler"]
