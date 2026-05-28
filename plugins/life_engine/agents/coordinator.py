"""智能体并行调度器。

管理后台智能体的异步执行和结果收集，
在心跳循环中注入已完成智能体的结果。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from .definitions import AgentResult
from .registry import get_agent_type_registry
from .runner import AgentRunner

if TYPE_CHECKING:
    from src.app.plugin_system.base import BasePlugin


class AgentCoordinator:
    """编排多个后台智能体的并行执行。"""

    def __init__(self, plugin: BasePlugin) -> None:
        self.plugin = plugin
        self._running: dict[str, asyncio.Task] = {}
        self._results: dict[str, AgentResult] = {}
        self._lock = asyncio.Lock()

    async def spawn(
        self,
        agent_type: str,
        task: str,
        context: str = "",
        name: str = "",
    ) -> str:
        """启动后台智能体，返回 agent_id。"""
        registry = get_agent_type_registry()
        type_def = registry.get(agent_type)
        if type_def is None:
            raise ValueError(f"未知智能体类型: {agent_type}")

        agent_id = name or f"{agent_type}_{uuid.uuid4().hex[:8]}"

        runner = AgentRunner(
            plugin=self.plugin,
            agent_type_def=type_def,
            task_prompt=task,
            context=context,
        )

        task_obj = asyncio.create_task(
            self._run_and_store(agent_id, runner),
            name=f"agent_{agent_id}",
        )

        async with self._lock:
            self._running[agent_id] = task_obj

        return agent_id

    async def collect_results(self, timeout_seconds: float = 60.0) -> dict[str, AgentResult]:
        """等待所有运行中的智能体完成，收集结果。"""
        async with self._lock:
            running = dict(self._running)

        if not running:
            return {}

        done, _ = await asyncio.wait(
            running.values(),
            timeout=timeout_seconds,
            return_when=asyncio.ALL_COMPLETED,
        )

        results: dict[str, AgentResult] = {}
        async with self._lock:
            for agent_id, task_obj in list(self._running.items()):
                if task_obj in done:
                    result = self._results.pop(agent_id, None)
                    if result:
                        results[agent_id] = result
                    del self._running[agent_id]

        return results

    def get_pending_count(self) -> int:
        """当前运行中的后台智能体数量。"""
        return len(self._running)

    def has_pending(self) -> bool:
        """是否有后台智能体正在运行。"""
        return bool(self._running)

    async def _run_and_store(self, agent_id: str, runner: AgentRunner) -> AgentResult:
        """执行智能体并存储结果。"""
        result = await runner.run()
        async with self._lock:
            self._results[agent_id] = result
        return result
