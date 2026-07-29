"""规划器：将爱莉的高层意图分解为结构化任务图。

支持两种模式：
- auto：调用 LLM 将自然语言目标分解为 TaskContract 列表
- manual：爱莉直接指定任务结构（跳过 LLM 分解）

规划器使用便宜模型（planner_task_name），输出结构化 JSON。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.logger import get_logger

from .contracts import (
    MissionBudget,
    PlanOutput,
    TaskBudget,
    TaskContract,
    TaskKind,
)

if TYPE_CHECKING:
    pass

logger = get_logger("life_engine.orchestration.planner")

# ---------------------------------------------------------------------------
# 规划器系统提示
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM_PROMPT = """\
你是一个任务规划器。你的职责是将一个高层目标分解为可并行执行的子任务图。

## 输出格式

严格输出 JSON（不要 markdown 代码块包裹），格式如下：
{
  "reasoning": "你的分解思路（简短）",
  "tasks": [
    {
      "id": "t1",
      "kind": "research | code | verify | synthesize | custom",
      "brief": "给 worker 的具体指令",
      "depends_on": [],
      "priority": 5
    },
    {
      "id": "t2",
      "kind": "code",
      "brief": "具体指令",
      "depends_on": ["t1"],
      "priority": 5
    }
  ]
}

## 规则

1. 每个 task 的 id 必须唯一，格式为 t1, t2, t3...
2. depends_on 引用同批次中其它 task 的 id（被依赖的任务先执行）
3. 无依赖的任务会被自动并行执行——尽量让独立任务不互相依赖
4. kind 决定 worker 的能力：
   - research: 只读，搜索检索信息
   - code: 读写，可以创建修改文件
   - verify: 只读，对抗性审查
   - synthesize: 只读，整合多源信息
   - custom: 读写，通用
5. brief 要具体、可执行，不要写模糊指令
6. 任务数量控制在 2-8 个，不要过度分解
7. 不要输出 JSON 以外的任何内容
"""


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class Planner:
    """将高层目标分解为 TaskContract 列表。"""

    def __init__(
        self,
        model_task_name: str = "sub_actor",
        max_tasks: int = 12,
    ) -> None:
        self.model_task_name = model_task_name
        self.max_tasks = max_tasks

    async def plan_auto(
        self,
        goal: str,
        mission_id: str,
        context: str = "",
        budget: MissionBudget | None = None,
    ) -> PlanOutput:
        """自动模式：调用 LLM 分解目标。"""
        model_set = get_model_set_by_task(self.model_task_name)
        if not model_set:
            raise RuntimeError(f"找不到规划器模型配置: {self.model_task_name}")

        user_prompt = self._build_planner_prompt(goal, context, budget)

        request = create_llm_request(
            model_set=model_set,
            request_name="life_engine_planner",
        )
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_PLANNER_SYSTEM_PROMPT)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        response = await request.send(stream=False)
        response_text = await response
        raw = str(response_text or "").strip()

        return self._parse_plan(raw, mission_id, budget)

    def plan_manual(
        self,
        tasks_raw: list[dict[str, Any]],
        mission_id: str,
        budget: MissionBudget | None = None,
    ) -> PlanOutput:
        """手动模式：从结构化字典列表构建 TaskContract。"""
        contracts: list[TaskContract] = []
        effective_budget = budget or MissionBudget()

        for i, raw in enumerate(tasks_raw):
            if i >= self.max_tasks:
                logger.warning(f"任务数超过上限 {self.max_tasks}，截断")
                break

            task_id = str(raw.get("id", "") or f"t{i + 1}").strip()
            kind_str = str(raw.get("kind", "custom") or "custom").strip().lower()
            try:
                kind = TaskKind(kind_str)
            except ValueError:
                kind = TaskKind.CUSTOM

            brief = str(raw.get("brief", "") or raw.get("task", "") or "").strip()
            if not brief:
                continue

            depends_raw = raw.get("depends_on", [])
            if isinstance(depends_raw, str):
                depends_raw = [depends_raw]
            depends_on = tuple(str(d).strip() for d in depends_raw if str(d).strip())

            priority = int(raw.get("priority", 5))
            priority = max(0, min(9, priority))

            task_budget = TaskBudget(
                max_rounds=int(raw.get("max_rounds", 12)),
                max_tokens_total=int(raw.get("max_tokens", 50_000)),
                max_duration_seconds=int(raw.get("timeout", 300)),
            )

            contracts.append(TaskContract(
                task_id=task_id,
                mission_id=mission_id,
                kind=kind,
                brief=brief,
                context=str(raw.get("context", "") or ""),
                depends_on=depends_on,
                budget=task_budget,
                priority=priority,
                timeout_seconds=task_budget.max_duration_seconds,
                retry_max=int(raw.get("retry_max", 2)),
            ))

        return PlanOutput(tasks=contracts, reasoning="manual specification")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_planner_prompt(
        self,
        goal: str,
        context: str,
        budget: MissionBudget | None,
    ) -> str:
        parts = [f"## 目标\n\n{goal.strip()}"]

        if context.strip():
            parts.append(f"\n## 背景信息\n\n{context.strip()}")

        if budget:
            parts.append(
                f"\n## 约束\n\n"
                f"- 最多 {budget.max_tasks} 个子任务\n"
                f"- 最大并行 {budget.max_concurrency} 个\n"
                f"- 全局时间预算 {budget.max_duration_seconds}s"
            )

        parts.append("\n请分解上述目标为子任务图。")
        return "\n".join(parts)

    def _parse_plan(
        self,
        raw: str,
        mission_id: str,
        budget: MissionBudget | None,
    ) -> PlanOutput:
        """解析 LLM 输出的 JSON 为 PlanOutput。"""
        # 尝试提取 JSON
        text = raw.strip()
        # 去掉可能的 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            # 去掉首尾的 ``` 行
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(f"规划器输出解析失败: {exc}\n原文: {raw[:500]}")
            # fallback: 单任务
            return self._fallback_single_task(raw, mission_id)

        reasoning = str(data.get("reasoning", "") or "")
        tasks_raw = data.get("tasks", [])
        if not isinstance(tasks_raw, list) or not tasks_raw:
            return self._fallback_single_task(raw, mission_id)

        # 用 manual 解析逻辑处理
        plan = self.plan_manual(tasks_raw, mission_id, budget)
        return PlanOutput(tasks=plan.tasks, reasoning=reasoning)

    def _fallback_single_task(self, goal: str, mission_id: str) -> PlanOutput:
        """解析失败时的降级：将整个目标作为单个 custom 任务。"""
        logger.warning("规划降级为单任务模式")
        task = TaskContract(
            task_id="t1",
            mission_id=mission_id,
            kind=TaskKind.CUSTOM,
            brief=goal[:2000],
            priority=5,
        )
        return PlanOutput(tasks=[task], reasoning="fallback: single task")
