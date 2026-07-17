"""智能体并行调度器。

管理后台智能体的异步执行和结果收集，
在心跳循环中注入已完成智能体的结果。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from .definitions import AgentResult, AgentTypeDefinition
from .registry import get_agent_type_registry
from .runner import AgentRunner

if TYPE_CHECKING:
    from src.app.plugin_system.base import BasePlugin
    from src.core.models.message import Message


class AgentCoordinator:
    """编排多个后台智能体的并行执行。"""

    def __init__(self, plugin: BasePlugin) -> None:
        self.plugin = plugin
        self._running: dict[str, asyncio.Task] = {}
        self._results: dict[str, AgentResult] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """协调器是否已停止，不再接受新的后台任务。"""
        return self._closed

    async def spawn(
        self,
        agent_type: str,
        task: str,
        context: str = "",
        name: str = "",
        extra_mcp_server_names: list[str] | None = None,
        agent_type_def: AgentTypeDefinition | None = None,
        stream_id: str = "",
        trigger_message: Message | None = None,
    ) -> str:
        """启动后台智能体，返回 agent_id。"""
        if agent_type_def is None:
            registry = get_agent_type_registry()
            type_def = registry.get(agent_type)
            if type_def is None:
                raise ValueError(f"未知智能体类型: {agent_type}")
        else:
            type_def = agent_type_def
            if type_def.agent_type != agent_type:
                raise ValueError(
                    f"智能体类型不匹配: {agent_type} != {type_def.agent_type}"
                )

        agent_id = name or f"{agent_type}_{uuid.uuid4().hex[:8]}"
        runner = AgentRunner(
            plugin=self.plugin,
            agent_type_def=type_def,
            task_prompt=task,
            context=context,
            extra_mcp_server_names=extra_mcp_server_names,
            stream_id=stream_id,
            trigger_message=trigger_message,
        )

        async with self._lock:
            if self._closed:
                raise RuntimeError("后台智能体协调器已停止")
            if bool(getattr(self.plugin, "_agent_coordinator_shutdown", False)):
                raise RuntimeError("插件正在停止，不能启动后台智能体")
            if agent_id in self._running:
                raise ValueError(f"后台智能体 ID 已存在: {agent_id}")
            task_obj = asyncio.create_task(
                self._run_and_store(agent_id, runner),
                name=f"agent_{agent_id}",
            )
            self._running[agent_id] = task_obj

        return agent_id

    async def collect_results(self, timeout_seconds: float = 60.0) -> dict[str, AgentResult]:
        """等待所有运行中的智能体完成，收集结果。"""
        async with self._lock:
            if self._closed:
                return {}
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

    async def shutdown(self) -> None:
        """取消所有后台任务，并禁止该协调器继续接收任务。"""
        async with self._lock:
            self._closed = True
            tasks = list(self._running.values())
            self._running.clear()
            self._results.clear()

        for task_obj in tasks:
            if not task_obj.done():
                task_obj.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_pending_count(self) -> int:
        """当前运行中的后台智能体数量。"""
        return len(self._running)

    def has_pending(self) -> bool:
        """是否有后台智能体正在运行。"""
        return bool(self._running)

    async def _run_and_store(self, agent_id: str, runner: AgentRunner) -> AgentResult:
        """执行智能体并存储结果。"""
        result = await runner.run()
        current_task = asyncio.current_task()
        async with self._lock:
            if (
                not self._closed
                and self._running.get(agent_id) is current_task
            ):
                self._results[agent_id] = result
        return result
