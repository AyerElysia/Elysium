"""编排系统类型契约。

定义使命（Mission）、任务（Task）、预算（Budget）、结果（Result）等
所有编排层共享的数据结构。所有类型均为不可变值对象。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class TaskKind(str, Enum):
    """任务类型——决定 worker 的工具集和系统提示。"""

    RESEARCH = "research"
    CODE = "code"
    VERIFY = "verify"
    SYNTHESIZE = "synthesize"
    CUSTOM = "custom"


class TaskStatus(str, Enum):
    """任务生命周期状态。"""

    PENDING = "pending"
    READY = "ready"          # 依赖已满足，等待调度
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"      # 因上游失败被跳过


class MissionStatus(str, Enum):
    """使命整体状态。"""

    PLANNING = "planning"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"      # 部分任务失败
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class FailurePolicy(str, Enum):
    """部分失败时的处理策略。"""

    FAIL_FAST = "fail_fast"              # 任一失败立即终止全部
    CONTINUE_OTHERS = "continue_others"  # 继续无依赖的其它任务
    RETRY_THEN_SKIP = "retry_then_skip"  # 重试后跳过，继续下游


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskBudget:
    """单个任务的资源预算。"""

    max_rounds: int = 12
    max_tokens_total: int = 50_000
    max_duration_seconds: int = 300


@dataclass(frozen=True, slots=True)
class MissionBudget:
    """整个使命的全局预算。"""

    max_tokens_total: int = 200_000
    max_duration_seconds: int = 1800
    max_tasks: int = 12
    max_concurrency: int = 4


# ---------------------------------------------------------------------------
# 任务契约
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass(frozen=True, slots=True)
class TaskContract:
    """任务的类型化契约——描述一个 worker 需要完成的工作单元。"""

    task_id: str
    mission_id: str
    kind: TaskKind
    brief: str                          # 给 worker 的指令
    context: str = ""                   # 背景信息
    output_schema: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()    # 依赖的 task_id
    budget: TaskBudget = field(default_factory=TaskBudget)
    priority: int = 5                   # 0=最高, 9=最低
    timeout_seconds: int = 300
    retry_max: int = 2

    @staticmethod
    def new_id() -> str:
        return _new_id("task")


# ---------------------------------------------------------------------------
# 任务结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskResult:
    """单个任务的执行结果。"""

    task_id: str
    status: TaskStatus
    output: dict[str, Any] | str = ""
    error: str | None = None
    rounds_used: int = 0
    tokens_used: int = 0
    duration_ms: int = 0
    trace_id: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == TaskStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 使命
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Mission:
    """一次使命——爱莉下达的一个高层目标及其全部任务图。"""

    mission_id: str
    goal: str
    status: MissionStatus = MissionStatus.PLANNING
    tasks: dict[str, TaskContract] = field(default_factory=dict)
    results: dict[str, TaskResult] = field(default_factory=dict)
    budget: MissionBudget = field(default_factory=MissionBudget)
    failure_policy: FailurePolicy = FailurePolicy.CONTINUE_OTHERS
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    trace_id: str = ""
    sync: bool = False                  # 是否同步等待

    @staticmethod
    def new_id() -> str:
        return _new_id("mission")

    # -- 便捷查询 --

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.created_at

    @property
    def total_tokens_used(self) -> int:
        return sum(r.tokens_used for r in self.results.values())

    @property
    def progress(self) -> tuple[int, int]:
        """(已完成任务数, 总任务数)"""
        terminal = {
            TaskStatus.SUCCEEDED, TaskStatus.FAILED,
            TaskStatus.CANCELLED, TaskStatus.TIMEOUT, TaskStatus.SKIPPED,
        }
        done = sum(1 for r in self.results.values() if r.status in terminal)
        return done, len(self.tasks)

    def summary_text(self) -> str:
        """生成人类可读的摘要。"""
        done, total = self.progress
        lines = [
            f"使命 {self.mission_id}: {self.goal[:80]}",
            f"状态: {self.status.value} | 进度: {done}/{total} | "
            f"耗时: {self.elapsed_seconds:.1f}s | token: {self.total_tokens_used}",
        ]
        for tid, res in self.results.items():
            mark = "✓" if res.ok else "✗" if res.status == TaskStatus.FAILED else "…"
            lines.append(f"  {mark} {tid}: {res.status.value}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 规划器输出
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanOutput:
    """规划器的结构化输出。"""

    tasks: list[TaskContract]
    reasoning: str = ""     # 规划器的思考过程（调试用）
