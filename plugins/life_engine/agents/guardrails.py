"""护栏系统：编排系统的安全边界。

提供多层护栏：
- 输入护栏：验证任务简报合法性（长度、注入检测）
- 输出护栏：验证 worker 输出基本合理性
- 预算护栏：全局资源上限守卫
- 安全护栏：写操作路径白名单
- 递归护栏：禁止 worker 再启动子代理（在 worker.py 的工具过滤中已实现）

护栏是轻量级、确定性的检查——不调用 LLM，不引入延迟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.kernel.logger import get_logger

from .contracts import MissionBudget, TaskContract

logger = get_logger("life_engine.orchestration.guardrails")


# ---------------------------------------------------------------------------
# 护栏结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    """护栏检查结果。"""

    passed: bool
    reason: str = ""

    @staticmethod
    def ok() -> "GuardrailVerdict":
        return GuardrailVerdict(passed=True)

    @staticmethod
    def reject(reason: str) -> "GuardrailVerdict":
        return GuardrailVerdict(passed=False, reason=reason)


# ---------------------------------------------------------------------------
# 输入护栏
# ---------------------------------------------------------------------------

# 任务简报最大长度
_MAX_BRIEF_LENGTH = 5000

# 简易注入检测模式（不是安全银弹，只是过滤最明显的尝试）
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?above", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an|the)\s+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*/?\s*system\s*>", re.IGNORECASE),
]


def check_input(task: TaskContract) -> GuardrailVerdict:
    """验证任务简报的合法性。"""
    brief = task.brief.strip()

    # 非空
    if not brief:
        return GuardrailVerdict.reject("任务简报为空")

    # 长度
    if len(brief) > _MAX_BRIEF_LENGTH:
        return GuardrailVerdict.reject(
            f"任务简报过长（{len(brief)} > {_MAX_BRIEF_LENGTH}）"
        )

    # 注入检测
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(brief):
            return GuardrailVerdict.reject(
                f"任务简报包含可疑注入模式: {pattern.pattern}"
            )

    return GuardrailVerdict.ok()


# ---------------------------------------------------------------------------
# 输出护栏
# ---------------------------------------------------------------------------

_MAX_OUTPUT_LENGTH = 50_000  # 单个任务输出的最大字符数


def check_output(output: dict[str, Any] | str) -> GuardrailVerdict:
    """验证 worker 输出的基本合理性。"""
    if output is None:
        return GuardrailVerdict.reject("输出为 None")

    text = str(output)
    if not text.strip():
        return GuardrailVerdict.reject("输出为空")

    if len(text) > _MAX_OUTPUT_LENGTH:
        return GuardrailVerdict.reject(
            f"输出过长（{len(text)} > {_MAX_OUTPUT_LENGTH}）"
        )

    return GuardrailVerdict.ok()


# ---------------------------------------------------------------------------
# 预算护栏
# ---------------------------------------------------------------------------


def check_mission_budget(
    total_tokens: int,
    elapsed_seconds: float,
    task_count: int,
    budget: MissionBudget,
) -> GuardrailVerdict:
    """检查使命级预算是否超限。"""
    if total_tokens >= budget.max_tokens_total:
        return GuardrailVerdict.reject(
            f"token 预算耗尽: {total_tokens} >= {budget.max_tokens_total}"
        )

    if elapsed_seconds >= budget.max_duration_seconds:
        return GuardrailVerdict.reject(
            f"时间预算耗尽: {elapsed_seconds:.0f}s >= {budget.max_duration_seconds}s"
        )

    if task_count > budget.max_tasks:
        return GuardrailVerdict.reject(
            f"任务数超限: {task_count} > {budget.max_tasks}"
        )

    return GuardrailVerdict.ok()


# ---------------------------------------------------------------------------
# 安全护栏：写操作路径检查
# ---------------------------------------------------------------------------

# 允许写入的路径前缀（相对于 workspace）
_WRITE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "data/",
    "plugins/life_engine/",
    "workspace/",
    "output/",
    "tmp/",
)

# 绝对禁止写入的路径
_WRITE_FORBIDDEN: tuple[str, ...] = (
    "/etc/",
    "/usr/",
    "/bin/",
    "/sbin/",
    "/boot/",
    "~/.ssh/",
    ".env",
    "credentials",
    "secret",
)


def check_write_path(file_path: str, workspace: str) -> GuardrailVerdict:
    """检查写操作的目标路径是否合法。"""
    normalized = str(file_path or "").strip()
    if not normalized:
        return GuardrailVerdict.reject("写入路径为空")

    # 绝对路径禁止列表
    lower = normalized.lower()
    for forbidden in _WRITE_FORBIDDEN:
        if forbidden in lower:
            return GuardrailVerdict.reject(f"写入路径命中禁止规则: {forbidden}")

    # 如果是绝对路径，必须在 workspace 内
    if normalized.startswith("/"):
        workspace_resolved = str(Path(workspace).resolve())
        path_resolved = str(Path(normalized).resolve())
        if not path_resolved.startswith(workspace_resolved):
            return GuardrailVerdict.reject(
                f"写入路径超出工作空间: {normalized}"
            )

    return GuardrailVerdict.ok()


# ---------------------------------------------------------------------------
# 任务图护栏
# ---------------------------------------------------------------------------


def check_task_count(count: int, budget: MissionBudget) -> GuardrailVerdict:
    """检查任务数量是否在预算内。"""
    if count <= 0:
        return GuardrailVerdict.reject("任务数为零")
    if count > budget.max_tasks:
        return GuardrailVerdict.reject(
            f"任务数 {count} 超过上限 {budget.max_tasks}"
        )
    return GuardrailVerdict.ok()
