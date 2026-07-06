"""智能体类型注册表。

管理所有已注册的智能体类型定义，提供类型查询和工具过滤。
遵循 life_engine 的 ServiceRegistry 单例模式。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .definitions import AgentTypeDefinition

if TYPE_CHECKING:
    from src.app.plugin_system.base import BaseTool


# 写入能力工具——只读智能体应排除
_WRITE_TOOL_NAMES: frozenset[str] = frozenset({
    "nucleus_write_file",
    "nucleus_edit_file",
    "nucleus_mkdir",
    "nucleus_bash",
    "nucleus_manage_todo",
    "nucleus_manage_schedule",
    "nucleus_manage_skill",
})

# 全局禁用——子智能体不应递归调用自身或直接对外沟通
_UNIVERSAL_DISALLOW: frozenset[str] = frozenset({
    "nucleus_run_agent",
    "nucleus_tell_dfc",
})


class AgentTypeRegistry:
    """线程安全的智能体类型注册表。"""

    def __init__(self) -> None:
        self._types: dict[str, AgentTypeDefinition] = {}
        self._lock = threading.Lock()

    def register(self, definition: AgentTypeDefinition) -> None:
        with self._lock:
            if definition.agent_type in self._types:
                raise RuntimeError(
                    f"Agent type already registered: {definition.agent_type}"
                )
            self._types[definition.agent_type] = definition

    def get(self, agent_type: str) -> AgentTypeDefinition | None:
        with self._lock:
            return self._types.get(agent_type)

    def list_active(self) -> list[AgentTypeDefinition]:
        with self._lock:
            return list(self._types.values())

    def filter_tools_for_agent(
        self,
        agent_type: str,
        available_tools: list[type[BaseTool]],
    ) -> list[type[BaseTool]]:
        """根据智能体类型定义过滤可用工具列表。

        过滤优先级：
        1. 全局禁用（_UNIVERSAL_DISALLOW）
        2. 类型级禁用（disallowed_tools）
        3. 只读限制（is_read_only → 排除写入工具）
        4. 类型级白名单（allowed_tools 非 None 时取交集）
        """
        defn = self.get(agent_type)
        if defn is None:
            return available_tools

        excluded = _UNIVERSAL_DISALLOW | set(defn.disallowed_tools)
        if defn.is_read_only:
            excluded = excluded | _WRITE_TOOL_NAMES

        allowed_set: set[str] | None = None
        if defn.allowed_tools is not None:
            allowed_set = set(defn.allowed_tools)

        result: list[type[BaseTool]] = []
        for tool_cls in available_tools:
            name = getattr(tool_cls, "tool_name", None) or ""
            if name in excluded:
                continue
            if allowed_set is not None and name not in allowed_set:
                continue
            result.append(tool_cls)
        return result


# 全局单例
_registry: AgentTypeRegistry = AgentTypeRegistry()


def get_agent_type_registry() -> AgentTypeRegistry:
    return _registry
