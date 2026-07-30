"""调度引擎：编排系统的核心执行循环。

职责：
- 从 TaskGraph 中取出就绪任务，按并发上限并行执行
- 重试策略（指数退避）
- 超时控制（任务级 + 使命级）
- 取消传播（级联取消所有子任务）
- 部分失败策略（fail_fast / continue_others / retry_then_skip）
- 全局预算守卫（token / 时间）
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from src.kernel.logger import get_logger

from .contracts import (
    FailurePolicy,
    Mission,
    MissionStatus,
    TaskContract,
    TaskResult,
    TaskStatus,
)
from .task_graph import TaskGraph
from .worker import Worker, TraceHook

if TYPE_CHECKING:
    from src.app.plugin_system.base import BasePlugin
    from src.core.models.message import Message

logger = get_logger("life_engine.orchestration.scheduler")


class Scheduler:
    """驱动一个 Mission 的任务图执行。"""

    def __init__(
        self,
        plugin: BasePlugin,
        mission: Mission,
        graph: TaskGraph,
        *,
        max_concurrency: int = 4,
        worker_task_name: str = "agent",
        retry_max_attempts: int = 2,
        retry_backoff_base: float = 2.0,
        failure_policy: FailurePolicy = FailurePolicy.CONTINUE_OTHERS,
        stream_id: str = "",
        trigger_message: Message | None = None,
        trace_hook: TraceHook | None = None,
    ) -> None:
        self.plugin = plugin
        self.mission = mission
        self.graph = graph
        self.max_concurrency = max_concurrency
        self.worker_task_name = worker_task_name
        self.retry_max_attempts = retry_max_attempts
        self.retry_backoff_base = retry_backoff_base
        self.failure_policy = failure_policy
        self.stream_id = stream_id
        self.trigger_message = trigger_message
        self.trace_hook = trace_hook

        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cancelled = False
        self._fail_fast_triggered = False
        self._running_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def run(self) -> Mission:
        """执行整个使命，返回更新后的 Mission。"""
        self.mission.status = MissionStatus.RUNNING
        logger.info(
            f"[{self.mission.mission_id}] 开始执行，"
            f"共 {self.graph.total_count} 个任务，并发上限 {self.max_concurrency}"
        )

        try:
            await asyncio.wait_for(
                self._main_loop(),
                timeout=self.mission.budget.max_duration_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[{self.mission.mission_id}] 使命全局超时")
            await self._cancel_running_tasks()
            self._record_graph_terminal_results(
                self.graph.cascade_cancel(),
                TaskStatus.CANCELLED,
                error="使命全局超时",
            )
            self.mission.status = MissionStatus.TIMEOUT
        except asyncio.CancelledError:
            await self._cancel_running_tasks()
            self._record_graph_terminal_results(
                self.graph.cascade_cancel(),
                TaskStatus.CANCELLED,
                error="使命执行协程已取消",
            )
            self.mission.status = MissionStatus.CANCELLED
            raise

        if self._running_tasks:
            await self._cancel_running_tasks()
        self._record_missing_terminal_results()
        if self.mission.status == MissionStatus.RUNNING:
            self.mission.status = self._compute_final_status()

        self.mission.finished_at = time.monotonic()
        logger.info(
            f"[{self.mission.mission_id}] 执行完毕: {self.mission.status.value}, "
            f"耗时 {self.mission.elapsed_seconds:.1f}s"
        )
        return self.mission

    def cancel(self) -> None:
        """请求取消使命。"""
        self._request_cancel(error="使命已取消")

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """调度主循环：不断取出就绪任务并行执行，直到图完成。"""
        while self.graph.has_pending() and not self._cancelled:
            # 全局预算检查
            if self._budget_exceeded():
                logger.warning(f"[{self.mission.mission_id}] 全局 token 预算耗尽")
                self._request_cancel(error="使命 token 预算耗尽")
                break

            ready = self.graph.get_ready_tasks()
            if not ready:
                # 没有就绪任务但还有 pending——可能是被阻塞或全部在 running
                if self._running_tasks:
                    # 等待任一完成
                    done, _ = await asyncio.wait(
                        self._running_tasks.values(),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    self._collect_done(done)
                    continue
                else:
                    # 死锁：无就绪、无运行中、但有 pending（不应发生）
                    logger.error(f"[{self.mission.mission_id}] 调度死锁，强制终止")
                    cancelled = self.graph.cascade_cancel()
                    self._record_graph_terminal_results(
                        cancelled,
                        TaskStatus.CANCELLED,
                        error="调度死锁",
                    )
                    break

            # 启动就绪任务
            batch = ready[:self.max_concurrency]
            for task_contract in batch:
                self.graph.set_status(task_contract.task_id, TaskStatus.RUNNING)
                asyncio_task = asyncio.create_task(
                    self._execute_with_semaphore(task_contract),
                    name=f"worker_{task_contract.task_id}",
                )
                self._running_tasks[task_contract.task_id] = asyncio_task

            # 等待至少一个完成
            if self._running_tasks:
                done, _ = await asyncio.wait(
                    self._running_tasks.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self._collect_done(done)

    def _collect_done(self, done: set[asyncio.Task]) -> None:
        """收集已完成的 asyncio.Task 结果。"""
        finished_ids = [
            tid for tid, t in self._running_tasks.items() if t in done
        ]
        for tid in finished_ids:
            task_obj = self._running_tasks.pop(tid)
            if task_obj.cancelled():
                self._record_terminal_result(
                    tid,
                    TaskStatus.CANCELLED,
                    error="任务执行已取消",
                )
                continue
            exception = task_obj.exception()
            if exception is not None:
                logger.error(f"[{tid}] worker 协程异常: {exception}")
                self.graph.set_status(tid, TaskStatus.FAILED)
                self._record_terminal_result(
                    tid,
                    TaskStatus.FAILED,
                    error=str(exception),
                )
                failed_task = self.graph.get_task(tid)
                if failed_task is not None:
                    self._handle_failure(failed_task)

    # ------------------------------------------------------------------
    # 单任务执行（含重试）
    # ------------------------------------------------------------------

    async def _execute_with_semaphore(self, task_contract: TaskContract) -> TaskResult:
        """在信号量控制下执行任务，含重试逻辑。"""
        async with self._semaphore:
            return await self._execute_with_retry(task_contract)

    async def _execute_with_retry(self, task_contract: TaskContract) -> TaskResult:
        """带指数退避重试的任务执行。"""
        max_attempts = max(1, task_contract.retry_max, self.retry_max_attempts)
        last_result: TaskResult | None = None

        for attempt in range(1, max_attempts + 1):
            if self._cancelled:
                return TaskResult(
                    task_id=task_contract.task_id,
                    status=TaskStatus.CANCELLED,
                    error="使命已取消",
                )

            worker = Worker(
                plugin=self.plugin,
                task=task_contract,
                model_task_name=self.worker_task_name,
                stream_id=self.stream_id,
                trigger_message=self.trigger_message,
                trace_hook=self.trace_hook,
                upstream_outputs=self._gather_upstream_outputs(task_contract),
            )

            result = await worker.run()
            result = TaskResult(
                task_id=result.task_id,
                status=result.status,
                output=result.output,
                error=result.error,
                rounds_used=result.rounds_used,
                tokens_used=result.tokens_used,
                duration_ms=result.duration_ms,
                trace_id=result.trace_id,
                attempts=attempt,
            )
            last_result = result

            # 存入 mission
            self.mission.results[task_contract.task_id] = result

            if result.ok:
                self.graph.set_status(task_contract.task_id, TaskStatus.SUCCEEDED)
                return result

            # 失败处理
            if attempt < max_attempts and not self._cancelled:
                backoff = self.retry_backoff_base ** (attempt - 1)
                logger.info(
                    f"[{task_contract.task_id}] 第 {attempt} 次失败，"
                    f"{backoff:.1f}s 后重试"
                )
                await asyncio.sleep(backoff)
            else:
                # 最终失败；保留 Worker 给出的精确终态（如 TIMEOUT）。
                terminal_status = (
                    result.status
                    if result.status in {TaskStatus.FAILED, TaskStatus.TIMEOUT}
                    else TaskStatus.FAILED
                )
                self.graph.set_status(task_contract.task_id, terminal_status)
                if terminal_status in {TaskStatus.FAILED, TaskStatus.TIMEOUT}:
                    self._handle_failure(task_contract)
                return result

        # 不应到达这里
        assert last_result is not None
        return last_result

    # ------------------------------------------------------------------
    # 失败策略
    # ------------------------------------------------------------------

    def _handle_failure(self, failed_task: TaskContract) -> None:
        """根据 failure_policy 处理任务失败。"""
        if self.failure_policy == FailurePolicy.FAIL_FAST:
            logger.warning(
                f"[{self.mission.mission_id}] fail_fast: 取消所有任务"
            )
            self._fail_fast_triggered = True
            self._request_cancel(
                error=f"上游任务失败: {failed_task.task_id}",
                exclude_task_id=failed_task.task_id,
            )

        elif self.failure_policy == FailurePolicy.CONTINUE_OTHERS:
            # 跳过直接依赖此任务的下游，但其它分支继续
            skipped = self.graph.cascade_skip(failed_task.task_id)
            self._record_graph_terminal_results(
                skipped,
                TaskStatus.SKIPPED,
                error=f"上游任务失败: {failed_task.task_id}",
            )
            if skipped:
                logger.info(
                    f"[{self.mission.mission_id}] 跳过 {len(skipped)} 个下游任务"
                )

        elif self.failure_policy == FailurePolicy.RETRY_THEN_SKIP:
            # 重试已在 _execute_with_retry 中处理，到这里说明重试也失败了
            skipped = self.graph.cascade_skip(failed_task.task_id)
            self._record_graph_terminal_results(
                skipped,
                TaskStatus.SKIPPED,
                error=f"上游任务重试失败: {failed_task.task_id}",
            )
            if skipped:
                logger.info(
                    f"[{self.mission.mission_id}] 重试失败后跳过 {len(skipped)} 个下游"
                )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _request_cancel(
        self,
        *,
        error: str,
        exclude_task_id: str = "",
    ) -> None:
        """同步请求取消，并立即为所有图终态补写可审计结果。"""
        self._cancelled = True
        for task_id, task_obj in self._running_tasks.items():
            if task_id != exclude_task_id and not task_obj.done():
                task_obj.cancel()
        cancelled = self.graph.cascade_cancel()
        self._record_graph_terminal_results(
            cancelled,
            TaskStatus.CANCELLED,
            error=error,
        )

    async def _cancel_running_tasks(self) -> None:
        """取消并回收所有运行中协程，避免超时后留下后台 Worker。"""
        pending = [task for task in self._running_tasks.values() if not task.done()]
        for task_obj in pending:
            task_obj.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._collect_done(set(self._running_tasks.values()))

    def _record_terminal_result(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: str | None = None,
    ) -> None:
        """在结果账本中补写未执行或被传播到终态的任务。"""
        existing = self.mission.results.get(task_id)
        if existing is not None:
            return
        self.mission.results[task_id] = TaskResult(
            task_id=task_id,
            status=status,
            error=error,
            attempts=0,
        )

    def _record_graph_terminal_results(
        self,
        task_ids: list[str],
        status: TaskStatus,
        *,
        error: str | None = None,
    ) -> None:
        for task_id in task_ids:
            self._record_terminal_result(task_id, status, error=error)

    def _record_missing_terminal_results(self) -> None:
        """保证 Mission.results 与图中的全部终态任务一一对应。"""
        for task_id in self.graph.task_ids:
            status = self.graph.get_status(task_id)
            if status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.TIMEOUT,
                TaskStatus.SKIPPED,
            }:
                self._record_terminal_result(task_id, status)

    def _gather_upstream_outputs(self, task: TaskContract) -> dict[str, Any]:
        """收集任务依赖的上游输出。"""
        outputs: dict[str, Any] = {}
        for dep_id in task.depends_on:
            dep_result = self.mission.results.get(dep_id)
            if dep_result and dep_result.ok:
                outputs[dep_id] = dep_result.output
        return outputs

    def _budget_exceeded(self) -> bool:
        """检查全局 token 预算。"""
        return self.mission.total_tokens_used >= self.mission.budget.max_tokens_total

    def _compute_final_status(self) -> MissionStatus:
        """根据所有任务结果计算使命最终状态。"""
        statuses = [
            self.graph.get_status(tid) for tid in self.graph.task_ids
        ]
        if all(s == TaskStatus.SUCCEEDED for s in statuses):
            return MissionStatus.SUCCEEDED
        if any(s == TaskStatus.SUCCEEDED for s in statuses):
            return MissionStatus.PARTIAL
        if self._fail_fast_triggered:
            return MissionStatus.FAILED
        if self._cancelled:
            return MissionStatus.CANCELLED
        return MissionStatus.FAILED
