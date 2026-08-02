"""BlockingPool 的契约测试。

被测的是三条性质，它们各自对应一个真实故障：

1. 阻塞调用必须离开事件循环——否则一个同步回调会让整个进程停住。
2. 超时后线程收不回来，这一点必须**可见**。``wait_for(to_thread(...))``
   的旧写法会静默泄漏一个全进程共用的 worker 槽位，泄漏在任何指标里都
   查不到，直到默认 executor 被占满、整个进程的 ``to_thread`` 一起挂死。
3. 线程池被占满与调用本身太慢是两种故障，必须能区分，否则排查会一直
   停在错误的调用上。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src.kernel.concurrency.blocking import (
    AbandonedCall,
    BlockingCallNotStarted,
    BlockingPool,
)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """轮询等待条件成立。

    Args:
        predicate: 无参谓词。
        timeout: 最长等待秒数。

    Returns:
        bool: 条件是否在超时前成立。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestBlockingPoolDispatch:
    """派发行为：调用必须在本池的线程里执行，且不占用事件循环。"""

    async def test_call_runs_on_pool_thread_not_event_loop(self) -> None:
        pool = BlockingPool("test-dispatch", max_workers=2)
        loop_thread = threading.current_thread().name

        def _who() -> str:
            return threading.current_thread().name

        try:
            worker_thread = await pool.run(_who)
        finally:
            pool.shutdown()

        assert worker_thread != loop_thread
        assert worker_thread.startswith("test-dispatch")

    async def test_event_loop_stays_responsive_during_blocking_call(self) -> None:
        """阻塞调用进行期间，事件循环上的其他任务必须继续被调度。"""
        pool = BlockingPool("test-responsive", max_workers=2)
        release = threading.Event()
        ticks = 0

        async def _tick() -> None:
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.01)
                ticks += 1
            release.set()

        try:
            ticker = asyncio.ensure_future(_tick())
            # 若 release.wait 跑在循环上，_tick 永远没机会推进，这里会死锁
            assert await pool.run(release.wait, (5.0,)) is True
            await ticker
        finally:
            pool.shutdown()

        assert ticks == 10

    async def test_arguments_and_exceptions_pass_through(self) -> None:
        pool = BlockingPool("test-args", max_workers=2)

        def _combine(a: int, *, b: int) -> int:
            return a + b

        def _boom() -> None:
            raise ValueError("原样传递")

        try:
            assert await pool.run(_combine, (1,), {"b": 2}) == 3
            with pytest.raises(ValueError, match="原样传递"):
                await pool.run(_boom)
        finally:
            pool.shutdown()


class TestBlockingPoolLeakAccounting:
    """超时核算：线程杀不掉，但泄漏必须记账。"""

    async def test_timeout_registers_abandoned_call_then_settles(self) -> None:
        pool = BlockingPool("test-abandon", max_workers=2)
        release = threading.Event()

        def _stuck() -> str:
            release.wait(10.0)
            return "终于返回"

        try:
            assert pool.stats()["abandoned_live"] == 0

            with pytest.raises(asyncio.TimeoutError):
                await pool.run(_stuck, timeout=0.05, label="stuck-call")

            # 线程仍在跑：泄漏必须立刻可见，而不是等它自己结束
            stats = pool.stats()
            assert stats["abandoned_live"] == 1
            assert stats["abandoned_total"] == 1
            assert stats["abandoned_labels"] == ["stuck-call"]

            # 调用最终返回后销账，但累计数不清零——它是历史记录
            release.set()
            assert _wait_until(lambda: pool.stats()["abandoned_live"] == 0)
            assert pool.stats()["abandoned_total"] == 1
        finally:
            release.set()
            pool.shutdown()

    async def test_saturated_pool_reports_not_started_distinctly(self) -> None:
        """池被占满时，排队中的调用超时必须与"跑太久"区分开。"""
        pool = BlockingPool("test-saturated", max_workers=1)
        release = threading.Event()
        started = threading.Event()

        def _hog() -> None:
            started.set()
            release.wait(10.0)

        def _never_runs() -> None:  # pragma: no cover - 断言它不会被执行
            raise AssertionError("排队中的调用不应被执行")

        try:
            first = asyncio.ensure_future(
                pool.run(_hog, timeout=0.05, label="hog")
            )
            with pytest.raises(asyncio.TimeoutError):
                await first
            assert started.wait(5.0)

            # 唯一的 worker 已经被无法中断的调用占住
            with pytest.raises(BlockingCallNotStarted) as excinfo:
                await pool.run(_never_runs, timeout=0.05, label="queued")
            assert "queued" in str(excinfo.value)

            # BlockingCallNotStarted 必须仍然是 TimeoutError，
            # 否则所有按超时处理的既有调用方都会漏掉这条路径
            assert isinstance(excinfo.value, asyncio.TimeoutError)

            # 没跑起来的调用不算泄漏
            assert pool.stats()["abandoned_labels"] == ["hog"]
        finally:
            release.set()
            pool.shutdown()

    async def test_no_timeout_waits_for_completion(self) -> None:
        pool = BlockingPool("test-untimed", max_workers=2)

        def _slow() -> str:
            time.sleep(0.1)
            return "完成"

        try:
            assert await pool.run(_slow, timeout=None) == "完成"
            assert pool.stats()["abandoned_total"] == 0
        finally:
            pool.shutdown()

    async def test_submission_queue_is_bounded(self) -> None:
        """Calls beyond worker plus queue capacity are rejected immediately."""
        pool = BlockingPool(
            "test-bounded",
            max_workers=1,
            max_queue_size=1,
        )
        release = threading.Event()
        started = threading.Event()

        def _hog() -> None:
            started.set()
            release.wait(5.0)

        try:
            first = asyncio.create_task(pool.run(_hog, timeout=None, label="first"))
            assert await asyncio.to_thread(started.wait, 1.0)
            second = asyncio.create_task(pool.run(_hog, timeout=None, label="second"))
            await asyncio.sleep(0)

            with pytest.raises(BlockingCallNotStarted, match="capacity|容量"):
                await pool.run(_hog, timeout=None, label="third")
            assert pool.stats()["rejected_total"] == 1

            release.set()
            await asyncio.gather(first, second)
        finally:
            release.set()
            pool.shutdown()


class TestBlockingPoolConfiguration:
    """配置解析：写错的调优参数必须在启动时炸掉，而不是被吞成另一种行为。"""

    def test_env_var_overrides_worker_count(self, monkeypatch) -> None:
        monkeypatch.setenv("ELYSIUM_TEST_POOL_WORKERS", "3")
        pool = BlockingPool(
            "test-env", max_workers=8, env_var="ELYSIUM_TEST_POOL_WORKERS"
        )
        try:
            assert pool.executor()._max_workers == 3
            assert pool.stats()["max_workers"] == 3
        finally:
            pool.shutdown()

    @pytest.mark.parametrize("value", ["0", "-1", "abc", "2.5"])
    def test_invalid_env_var_raises_instead_of_falling_back(
        self, monkeypatch, value: str
    ) -> None:
        monkeypatch.setenv("ELYSIUM_TEST_POOL_WORKERS", value)
        pool = BlockingPool(
            "test-env-bad", max_workers=8, env_var="ELYSIUM_TEST_POOL_WORKERS"
        )
        with pytest.raises(ValueError):
            pool.executor()

    def test_blank_env_var_uses_declared_default(self, monkeypatch) -> None:
        monkeypatch.setenv("ELYSIUM_TEST_POOL_WORKERS", "   ")
        pool = BlockingPool(
            "test-env-blank", max_workers=5, env_var="ELYSIUM_TEST_POOL_WORKERS"
        )
        try:
            assert pool.executor()._max_workers == 5
        finally:
            pool.shutdown()

    def test_abandoned_call_record_is_immutable(self) -> None:
        record = AbandonedCall(label="x", timeout=1.0, started_at=0.0)
        with pytest.raises(Exception):
            record.label = "y"  # type: ignore[misc]

    async def test_shutdown_is_idempotent_and_pool_reusable(self) -> None:
        pool = BlockingPool("test-shutdown", max_workers=2)
        assert await pool.run(lambda: 1) == 1
        pool.shutdown()
        pool.shutdown()
        # 关闭后重新使用应当重建线程池，而不是抛 RuntimeError
        try:
            assert await pool.run(lambda: 2) == 2
        finally:
            pool.shutdown()
