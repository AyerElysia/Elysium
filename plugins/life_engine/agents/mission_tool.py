"""使命编排工具：暴露给 life_chatter 的子代理编排接口。

提供三个工具：
- life_dispatch_mission：下达使命（核心工具）
- life_mission_status：查询使命进度
- life_mission_cancel：取消使命

爱莉通过这些工具管理她的子代理集团军——形成意图、下达任务、
接收结果、整合认知。重活由编排系统调度 worker 完成，不污染主体意识。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from src.app.plugin_system.base import BaseTool
from src.kernel.logger import get_logger

logger = get_logger("life_engine.orchestration.mission_tool")


# ---------------------------------------------------------------------------
# 全局使命注册表（进程内）
# ---------------------------------------------------------------------------

_active_missions: dict[str, Any] = {}  # mission_id → Mission
_active_schedulers: dict[str, Any] = {}  # mission_id → Scheduler
_background_tasks: dict[str, asyncio.Task] = {}  # mission_id → asyncio.Task


def get_mission(mission_id: str) -> Any | None:
    return _active_missions.get(mission_id)


def get_all_missions() -> dict[str, Any]:
    return dict(_active_missions)


# ---------------------------------------------------------------------------
# life_dispatch_mission
# ---------------------------------------------------------------------------


class LifeDispatchMissionTool(BaseTool):
    """使命编排工具：下达一个使命给子代理集团军。"""

    tool_name: str = "life_dispatch_mission"
    tool_description: str = (
        "下达一个使命（Mission）给子代理编排系统。"
        "编排系统会自动将目标分解为多个子任务，并行调度 worker 完成。"
        "\n\n"
        "**适用场景：**\n"
        "- 需要调研一件复杂的事情（多源搜索 + 综合分析）\n"
        "- 需要写代码实现一个功能（设计 + 实现 + 验证）\n"
        "- 任何需要多步骤、多工具协作的重型任务\n"
        "\n"
        "**模式：**\n"
        "- auto（默认）：编排系统调用规划器自动分解目标为子任务图\n"
        "- manual：你直接指定任务列表（tasks 参数），跳过自动分解\n"
        "\n"
        "**同步 vs 后台：**\n"
        "- sync=true（默认）：阻塞等待使命完成，直接拿到全部结果\n"
        "- sync=false：立即返回 mission_id，后台执行，"
        "结果会在完成后注入事件流\n"
        "\n"
        "**写任务简报的原则：**\n"
        "1. goal 说清要达成什么目标、为什么\n"
        "2. context 提供已知信息（文件路径、关键词、排除项）\n"
        "3. 不要写模糊指令——「帮我看看」是不合格的\n"
        "4. 子代理拥有独立上下文，看不到你的对话历史"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        goal: Annotated[
            str,
            "使命目标：要达成什么。要具体、可执行。",
        ],
        mode: Annotated[
            str,
            "规划模式：auto（LLM 自动分解）/ manual（直接指定 tasks）",
        ] = "auto",
        tasks: Annotated[
            list[dict[str, Any]],
            "manual 模式下的任务列表。每项: {id, kind, brief, depends_on, priority}",
        ] | None = None,
        context: Annotated[
            str,
            "背景信息：已知信息、文件路径、约束条件。",
        ] = "",
        sync: Annotated[
            bool,
            "true=同步等待结果；false=后台执行，立即返回 mission_id。",
        ] = True,
        max_concurrency: Annotated[
            int,
            "最大并行 worker 数。0 使用配置默认值。",
        ] = 0,
    ) -> tuple[bool, str | dict[str, Any]]:
        """下达使命。"""
        # 检查开关
        cfg = self._get_orchestration_config()
        if cfg is None or not getattr(cfg, "enabled", False):
            return False, "编排系统未启用（life_engine.orchestration.enabled=false）"

        goal_text = str(goal or "").strip()
        if not goal_text:
            return False, "goal 不能为空"
        if len(goal_text) > 3000:
            return False, "goal 过长（最大 3000 字符）"

        mode_normalized = str(mode or "auto").strip().lower()
        if mode_normalized not in ("auto", "manual"):
            return False, f"未知 mode: {mode}（应为 auto 或 manual）"

        if mode_normalized == "manual" and not tasks:
            return False, "manual 模式必须提供 tasks 参数"

        try:
            result = await self._dispatch(
                goal=goal_text,
                mode=mode_normalized,
                tasks=tasks or [],
                context=str(context or ""),
                sync=sync,
                max_concurrency=max_concurrency,
                cfg=cfg,
            )
            return True, result
        except Exception as exc:
            logger.error(f"使命下达失败: {exc}", exc_info=True)
            return False, f"使命下达失败: {exc}"

    async def _dispatch(
        self,
        goal: str,
        mode: str,
        tasks: list[dict[str, Any]],
        context: str,
        sync: bool,
        max_concurrency: int,
        cfg: Any,
    ) -> dict[str, Any]:
        """核心调度逻辑。"""
        from .contracts import (
            FailurePolicy,
            Mission,
            MissionBudget,
            TaskStatus,
        )
        from .guardrails import check_input, check_task_count
        from .planner import Planner
        from .scheduler import Scheduler
        from .task_graph import TaskGraph
        from .tracing import MissionTracer

        # 构建预算
        effective_concurrency = (
            max_concurrency if max_concurrency > 0
            else getattr(cfg, "max_concurrency", 4)
        )
        budget = MissionBudget(
            max_tokens_total=getattr(cfg, "max_tokens_per_mission", 200_000),
            max_duration_seconds=getattr(cfg, "max_mission_duration_seconds", 1800),
            max_tasks=getattr(cfg, "max_tasks_per_mission", 12),
            max_concurrency=effective_concurrency,
        )

        # 创建使命
        mission = Mission(
            mission_id=Mission.new_id(),
            goal=goal,
            budget=budget,
            failure_policy=FailurePolicy(
                getattr(cfg, "failure_policy", "continue_others")
            ),
            sync=sync,
        )

        # 追踪器
        workspace = self._get_workspace_path()
        tracer = MissionTracer(
            workspace_path=workspace,
            mission_id=mission.mission_id,
            enabled=getattr(cfg, "trace_enabled", True),
        )

        # 规划
        planner = Planner(
            model_task_name=getattr(cfg, "planner_task_name", "agent"),
            max_tasks=budget.max_tasks,
            plugin=self.plugin,
            stream_id=self.get_current_stream_id(),
            trigger_message=self.trigger_message,
        )

        if mode == "auto":
            plan = await planner.plan_auto(
                goal=goal,
                mission_id=mission.mission_id,
                context=context,
                budget=budget,
            )
        else:
            plan = planner.plan_manual(tasks, mission.mission_id, budget)

        # 护栏：任务数量
        verdict = check_task_count(len(plan.tasks), budget)
        if not verdict.passed:
            return {"error": f"规划护栏拒绝: {verdict.reason}"}

        # 护栏：每个任务的输入
        for task in plan.tasks:
            v = check_input(task)
            if not v.passed:
                return {"error": f"任务 {task.task_id} 输入护栏拒绝: {v.reason}"}

        # 构建任务图
        graph = TaskGraph()
        graph.add_tasks(plan.tasks)
        for task in plan.tasks:
            mission.tasks[task.task_id] = task

        tracer.trace_mission_start(goal, len(plan.tasks), {
            "mode": mode,
            "max_concurrency": effective_concurrency,
            "failure_policy": mission.failure_policy.value,
        })
        tracer.trace_plan(plan.reasoning, [t.task_id for t in plan.tasks])

        # 注册
        _active_missions[mission.mission_id] = mission

        # 调度器
        scheduler = Scheduler(
            plugin=self.plugin,
            mission=mission,
            graph=graph,
            max_concurrency=effective_concurrency,
            worker_task_name=getattr(cfg, "worker_task_name", "agent"),
            retry_max_attempts=getattr(cfg, "retry_max_attempts", 2),
            retry_backoff_base=getattr(cfg, "retry_backoff_base", 2.0),
            failure_policy=mission.failure_policy,
            stream_id=self.get_current_stream_id(),
            trigger_message=self.trigger_message,
            trace_hook=tracer.as_trace_hook(),
        )
        _active_schedulers[mission.mission_id] = scheduler

        if sync:
            # 同步执行
            try:
                await scheduler.run()
            finally:
                _active_schedulers.pop(mission.mission_id, None)

            tracer.trace_mission_end(
                status=mission.status.value,
                total_duration_ms=int(mission.elapsed_seconds * 1000),
                total_tokens=mission.total_tokens_used,
                tasks_succeeded=sum(
                    1 for r in mission.results.values() if r.ok
                ),
                tasks_failed=sum(
                    1 for r in mission.results.values()
                    if r.status == TaskStatus.FAILED
                ),
            )
            return self._format_sync_result(mission)
        else:
            # 后台执行
            bg_task = asyncio.create_task(
                self._run_background(scheduler, mission, tracer),
                name=f"mission_{mission.mission_id}",
            )
            _background_tasks[mission.mission_id] = bg_task

            return {
                "action": "mission_dispatched_background",
                "mission_id": mission.mission_id,
                "goal": goal[:200],
                "task_count": len(plan.tasks),
                "max_concurrency": effective_concurrency,
                "status": "running",
                "hint": "使命在后台执行。用 life_mission_status 查询进度。",
            }

    async def _run_background(self, scheduler: Any, mission: Any, tracer: Any) -> None:
        """后台执行使命。"""
        from .contracts import TaskStatus

        try:
            await scheduler.run()
        except Exception as exc:
            logger.error(f"后台使命 {mission.mission_id} 异常: {exc}", exc_info=True)
        finally:
            _active_schedulers.pop(mission.mission_id, None)
            _background_tasks.pop(mission.mission_id, None)

            tracer.trace_mission_end(
                status=mission.status.value,
                total_duration_ms=int(mission.elapsed_seconds * 1000),
                total_tokens=mission.total_tokens_used,
                tasks_succeeded=sum(
                    1 for r in mission.results.values() if r.ok
                ),
                tasks_failed=sum(
                    1 for r in mission.results.values()
                    if r.status == TaskStatus.FAILED
                ),
            )

    @staticmethod
    def _format_sync_result(mission: Any) -> dict[str, Any]:
        """格式化同步执行的结果。"""
        task_results = []
        for tid, res in mission.results.items():
            task_results.append({
                "task_id": tid,
                "status": res.status.value,
                "output": res.output if res.ok else res.error,
                "rounds": res.rounds_used,
                "duration_ms": res.duration_ms,
            })

        return {
            "action": "mission_completed",
            "mission_id": mission.mission_id,
            "status": mission.status.value,
            "goal": mission.goal[:200],
            "elapsed_seconds": round(mission.elapsed_seconds, 1),
            "total_tokens": mission.total_tokens_used,
            "tasks": task_results,
        }

    def _get_orchestration_config(self) -> Any:
        plugin_config = getattr(self.plugin, "config", None)
        return getattr(plugin_config, "orchestration", None) if plugin_config else None

    def _get_workspace_path(self) -> str:
        plugin_config = getattr(self.plugin, "config", None)
        if plugin_config:
            settings = getattr(plugin_config, "settings", None)
            if settings:
                return str(getattr(settings, "workspace_path", "") or "")
        return ""


# ---------------------------------------------------------------------------
# life_mission_status
# ---------------------------------------------------------------------------


class LifeMissionStatusTool(BaseTool):
    """查询使命执行进度。"""

    tool_name: str = "life_mission_status"
    tool_description: str = (
        "查询一个正在执行或已完成的使命（Mission）的状态和进度。"
        "返回各子任务的执行状态、已产出的中间结果。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        mission_id: Annotated[str, "要查询的使命 ID"],
    ) -> tuple[bool, str | dict[str, Any]]:
        mission = _active_missions.get(str(mission_id or "").strip())
        if mission is None:
            return False, f"未找到使命: {mission_id}"

        done, total = mission.progress
        task_details = []
        for tid, res in mission.results.items():
            task_details.append({
                "task_id": tid,
                "status": res.status.value,
                "output_preview": str(res.output)[:200] if res.ok else res.error,
            })

        # 尚未有结果的任务
        for tid in mission.tasks:
            if tid not in mission.results:
                from .contracts import TaskStatus
                graph_status = TaskStatus.PENDING.value
                task_details.append({
                    "task_id": tid,
                    "status": graph_status,
                    "output_preview": None,
                })

        return True, {
            "mission_id": mission.mission_id,
            "goal": mission.goal[:200],
            "status": mission.status.value,
            "progress": f"{done}/{total}",
            "elapsed_seconds": round(mission.elapsed_seconds, 1),
            "total_tokens": mission.total_tokens_used,
            "tasks": task_details,
        }


# ---------------------------------------------------------------------------
# life_mission_cancel
# ---------------------------------------------------------------------------


class LifeMissionCancelTool(BaseTool):
    """取消正在执行的使命。"""

    tool_name: str = "life_mission_cancel"
    tool_description: str = (
        "取消一个正在执行的使命。所有未完成的子任务会被级联取消。"
        "已完成的任务结果保留。"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        mission_id: Annotated[str, "要取消的使命 ID"],
        reason: Annotated[str, "取消原因"] = "",
    ) -> tuple[bool, str | dict[str, Any]]:
        mid = str(mission_id or "").strip()
        mission = _active_missions.get(mid)
        if mission is None:
            return False, f"未找到使命: {mission_id}"

        scheduler = _active_schedulers.get(mid)
        if scheduler is None:
            return False, f"使命 {mid} 不在执行中（状态: {mission.status.value}）"

        scheduler.cancel()
        logger.info(f"使命 {mid} 已请求取消，原因: {reason or '未说明'}")

        return True, {
            "action": "mission_cancel_requested",
            "mission_id": mid,
            "reason": reason or "用户取消",
        }


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


class NucleusMissionTool(BaseTool):
    """统一的使命编排工具（合并原 dispatch/status/cancel 三个工具）。"""

    tool_name: str = "nucleus_mission"
    tool_description: str = (
        "使命编排系统：下达重型任务给子代理集团军。\n\n"
        "action=dispatch：下达新使命（编排系统自动分解为子任务图，并行调度 worker）\n"
        "action=status：查询使命执行进度\n"
        "action=cancel：取消正在执行的使命\n\n"
        "**适用场景：**\n"
        "- 复杂调研（多源搜索 + 综合分析）\n"
        "- 代码实现（设计 + 实现 + 验证）\n"
        "- 任何需要多步骤、多工具协作的重型任务\n\n"
        "**模式（dispatch）：**\n"
        "- auto（默认）：LLM 自动分解目标\n"
        "- manual：直接指定 tasks 列表\n\n"
        "**同步 vs 后台：**\n"
        "- sync=true（默认）：阻塞等待结果\n"
        "- sync=false：后台执行，立即返回 mission_id"
    )
    chatter_allow: list[str] = ["life_chatter"]

    async def execute(
        self,
        action: Annotated[str, "操作：dispatch/status/cancel"] = "dispatch",
        **kwargs: object,
    ) -> tuple[bool, str | dict[str, Any]]:
        action_value = str(action or "dispatch").strip().lower()
        if action_value in ("dispatch", "new", "create", "start"):
            tool = LifeDispatchMissionTool(plugin=self.plugin)
            return await tool.execute(**kwargs)  # type: ignore[arg-type]
        elif action_value in ("status", "query", "progress", "check"):
            tool = LifeMissionStatusTool(plugin=self.plugin)
            return await tool.execute(**kwargs)  # type: ignore[arg-type]
        elif action_value in ("cancel", "stop", "abort"):
            tool = LifeMissionCancelTool(plugin=self.plugin)
            return await tool.execute(**kwargs)  # type: ignore[arg-type]
        return False, f"未知操作: {action_value}，可用: dispatch/status/cancel"


MISSION_TOOLS: list[type[BaseTool]] = [
    NucleusMissionTool,
]
