"""阻塞调用的线程派发与超时核算。

事件循环上任何一次阻塞调用都会让同一循环上的全部任务一起停住，所以同步的
回调、处理器、文件与数据库操作必须换到线程里跑。常见写法是::

    await asyncio.wait_for(asyncio.to_thread(fn, ...), timeout=timeout)

它有两个必须正视的性质：

1. **超时不会结束线程。** ``wait_for`` 超时后取消的只是那个 awaitable。
   Python 没有、也不可能安全地强杀一个正在执行的线程，所以被调用方仍然占着
   worker 直到自己返回。协程侧记了一次超时，线程侧却泄漏了一个槽位——而且
   这个泄漏在任何指标里都看不见。

2. **泄漏的是全进程共用的槽位。** ``asyncio.to_thread`` 派发到解释器的默认
   executor，适配器读文件、媒体解码、向量检索共用同一个池子。一个卡死的调用
   因此不是局部故障，而是全进程 ``to_thread`` 的故障。

:class:`BlockingPool` 把两点分开处理：每个子系统持有自己的有界线程池，故障
半径收敛在子系统内部；超时后线程仍然收不回来，但它会被登记为"已放弃"，在
统计里可见、在最终返回时留下带时长的日志，而不是无声消失。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from src.kernel.logger import get_logger

logger = get_logger("kernel.concurrency", display="并发")


class BlockingCallNotStarted(TimeoutError):
    """超时发生时调用还没有被任何 worker 取走。

    与"调用跑太久"是两种不同的故障：这说明线程池本身已经没有可用容量，通常
    意味着此前已经有调用泄漏在池子里。它继承 :class:`TimeoutError`（在 3.11
    中即 ``asyncio.TimeoutError``），因此按超时处理的调用方无需改动；但日志和
    统计能把两者区分开，否则排查会一直停在错误的调用上。
    """


@dataclass(frozen=True)
class AbandonedCall:
    """一次超时后仍在运行的阻塞调用。

    Attributes:
        label: 调用方给出的标识，用于定位是谁不肯返回。
        timeout: 它已经超过的超时时间（秒）。
        started_at: ``time.monotonic()`` 时间戳，用于计算真实执行时长。
    """

    label: str
    timeout: float
    started_at: float


class BlockingPool:
    """一个具名的阻塞调用线程池。

    每个子系统应当持有自己的实例。共用一个池子会让一个子系统的阻塞调用饿死
    另一个子系统，这正是默认 executor 已经存在的问题。
    """

    def __init__(
        self,
        name: str,
        *,
        max_workers: int,
        env_var: str | None = None,
    ) -> None:
        """初始化线程池描述，线程在首次使用时才真正创建。

        Args:
            name: 池名，用作线程名前缀与日志标识。
            max_workers: 默认 worker 数。
            env_var: 可选的环境变量名，允许运维按实际负载覆盖 worker 数。
        """
        self._name = name
        self._default_max_workers = max_workers
        self._env_var = env_var

        self._executor: ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self._max_workers: int | None = None

        self._abandoned: dict[int, AbandonedCall] = {}
        self._abandoned_lock = threading.Lock()
        self._total_abandoned = 0

    @property
    def name(self) -> str:
        """池名。"""
        return self._name

    def _resolve_max_workers(self) -> int:
        """解析 worker 数。

        Returns:
            int: worker 数量，至少为 1。

        Raises:
            ValueError: 环境变量存在但不是正整数。此处不回退到默认值——一个
                写错的调优参数应当在启动时暴露，而不是被默默忽略成另一种行为。
        """
        if self._env_var is None:
            return self._default_max_workers

        raw = os.environ.get(self._env_var)
        if raw is None or not raw.strip():
            return self._default_max_workers

        try:
            workers = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"{self._env_var} 必须是正整数，实际为 {raw!r}"
            ) from exc

        if workers < 1:
            raise ValueError(f"{self._env_var} 必须是正整数，实际为 {workers}")
        return workers

    def executor(self) -> ThreadPoolExecutor:
        """返回底层线程池（首次使用时创建）。

        Returns:
            ThreadPoolExecutor: 有界线程池。
        """
        if self._executor is not None:
            return self._executor

        with self._executor_lock:
            if self._executor is None:
                self._max_workers = self._resolve_max_workers()
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix=self._name,
                )
            return self._executor

    def _register_abandoned(self, future: Future[Any], record: AbandonedCall) -> None:
        """登记一个超时后仍在运行的调用，并在它真正结束时销账。

        Args:
            future: 该调用对应的 future，仍在运行中。
            record: 调用的身份与计时信息。
        """
        key = id(future)
        with self._abandoned_lock:
            self._abandoned[key] = record
            self._total_abandoned += 1
            live = len(self._abandoned)

        capacity = self._max_workers or self._default_max_workers
        logger.warning(
            f"[{self._name}] {record.label} 超过 {record.timeout:.1f}s 仍未返回，"
            f"线程无法中断，已放弃等待（当前放弃中 {live}/{capacity}）"
        )
        if live >= capacity:
            logger.error(
                f"[{self._name}] 线程池已被 {live} 个无法中断的调用占满，"
                f"后续阻塞调用将无法启动；请修复这些调用的阻塞点"
            )

        def _settle(done: Future[Any]) -> None:
            """调用最终返回时销账并记录真实时长。"""
            with self._abandoned_lock:
                self._abandoned.pop(key, None)
                remaining = len(self._abandoned)

            elapsed = time.monotonic() - record.started_at
            try:
                error = done.exception()
            except BaseException:  # noqa: BLE001 - future 被取消等边界情况
                error = None

            if error is not None:
                logger.warning(
                    f"[{self._name}] 已放弃的 {record.label} 在 {elapsed:.1f}s 后"
                    f"以异常结束: {error}（剩余放弃中 {remaining}）"
                )
            else:
                logger.info(
                    f"[{self._name}] 已放弃的 {record.label} 在 {elapsed:.1f}s 后"
                    f"终于返回（剩余放弃中 {remaining}）"
                )

        future.add_done_callback(_settle)

    async def run(
        self,
        fn: Callable[..., Any],
        args: Sequence[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        label: str = "",
    ) -> Any:
        """在本池中执行阻塞调用，并施加超时。

        Args:
            fn: 同步可调用对象。
            args: 位置参数。
            kwargs: 关键字参数。
            timeout: 超时秒数；``None`` 或非正数表示不限时。
            label: 日志与统计中使用的标识，通常是任务名或处理器名。

        Returns:
            Any: ``fn`` 的返回值。

        Raises:
            BlockingCallNotStarted: 超时时调用尚未获得 worker（线程池已满）。
            asyncio.TimeoutError: 调用已开始执行但超时；线程被登记为放弃中。
            Exception: ``fn`` 自身抛出的任何异常，原样向上传递。
        """
        started_at = time.monotonic()
        future = self.executor().submit(fn, *args, **(kwargs or {}))
        awaitable = asyncio.wrap_future(future)

        try:
            if timeout is None or timeout <= 0:
                return await awaitable
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # cancel() 为真是唯一可靠的"调用绝不会执行"的证据：它只在 future
            # 仍在队列里时成功。为假则调用已经在跑，线程收不回来。
            if future.cancel():
                raise BlockingCallNotStarted(
                    f"[{self._name}] {label or fn!r} 在 "
                    f"{timeout if timeout else 0:.1f}s 内未能获得线程池 worker"
                ) from None
            self._register_abandoned(
                future,
                AbandonedCall(
                    label=label or repr(fn),
                    timeout=float(timeout or 0.0),
                    started_at=started_at,
                ),
            )
            raise

    def stats(self) -> dict[str, Any]:
        """返回线程池运行状态。

        Returns:
            dict[str, Any]: 包含 ``name``、``max_workers``（未创建时为 ``None``）、
            ``abandoned_live``（当前无法中断的调用数）、``abandoned_total``
            （进程累计）与 ``abandoned_labels``（当前放弃中的标识）。
        """
        with self._abandoned_lock:
            live = list(self._abandoned.values())
            total = self._total_abandoned

        return {
            "name": self._name,
            "max_workers": self._max_workers,
            "abandoned_live": len(live),
            "abandoned_total": total,
            "abandoned_labels": sorted(record.label for record in live),
        }

    def shutdown(self, *, wait: bool = False) -> None:
        """关闭线程池。

        Args:
            wait: 是否等待在途调用结束。默认为 ``False``——若存在放弃中的调用，
                等待意味着永久挂起，而关闭路径不能被一个坏调用劫持。排队中的
                调用无论如何都会被取消。
        """
        with self._executor_lock:
            executor = self._executor
            self._executor = None
            self._max_workers = None

        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
