"""life_engine 智能体类型系统与编排引擎。

提供可扩展的智能体类型定义、注册表和运行器，
以及生产级子代理编排系统（MissionControl）。
"""

from .definitions import AgentTypeDefinition, AgentResult
from .registry import AgentTypeRegistry, get_agent_type_registry
from .builtin import register_builtin_agents
from .contracts import (
    TaskKind,
    TaskStatus,
    MissionStatus,
    FailurePolicy,
    TaskBudget,
    MissionBudget,
    TaskContract,
    TaskResult,
    Mission,
    PlanOutput,
)
from .task_graph import TaskGraph, CycleDetectedError
from .worker import Worker
from .scheduler import Scheduler
from .planner import Planner
from .tracing import MissionTracer
from .mission_tool import MISSION_TOOLS

__all__ = [
    # 原有
    "AgentTypeDefinition",
    "AgentResult",
    "AgentTypeRegistry",
    "get_agent_type_registry",
    "register_builtin_agents",
    # 编排系统
    "TaskKind",
    "TaskStatus",
    "MissionStatus",
    "FailurePolicy",
    "TaskBudget",
    "MissionBudget",
    "TaskContract",
    "TaskResult",
    "Mission",
    "PlanOutput",
    "TaskGraph",
    "CycleDetectedError",
    "Worker",
    "Scheduler",
    "Planner",
    "MissionTracer",
    "MISSION_TOOLS",
]
