"""统一工具注册表。

本地工具 + MCP 远程工具统一注册、统一发现、统一执行。
调用方不需要知道工具来源。

用法：
    from src.kernel.mcp import tool_registry, ToolDefinition

    # 注册本地工具
    @tool_registry.tool(name="get_weather", description="获取天气")
    async def get_weather(city: str) -> str:
        return f"{city}：晴，25°C"

    # 列出所有工具（含 MCP）
    schemas = tool_registry.list_schemas()

    # 执行
    result = await tool_registry.execute("get_weather", {"city": "北京"})
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Coroutine

from ..logger import get_logger
from .protocol import ToolDefinition, ToolEntry, ToolFn

logger = get_logger("tool_registry", display="ToolRegistry", enable_event_broadcast=False)


class ToolRegistry:
    """统一工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}

    # ─── 注册 ────────────────────────────────

    def register(
        self,
        definition: ToolDefinition,
        fn: ToolFn | None = None,
        *,
        source: str = "local",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """注册工具。"""
        entry = ToolEntry(
            definition=definition,
            fn=fn,
            source=source,
            metadata=metadata or {},
        )
        self._tools[definition.name] = entry
        logger.debug(f"注册工具: {definition.name} (source={source})")

    def tool(
        self,
        *,
        name: str | None = None,
        description: str = "",
        schema: dict[str, Any] | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        """装饰器：注册一个本地工具。

        用法：
            @tool_registry.tool(name="search", description="搜索")
            async def search(query: str) -> str: ...
        """
        def decorator(fn: ToolFn) -> ToolFn:
            tool_name = name or fn.__name__
            input_schema = schema or _infer_schema(fn)
            defn = ToolDefinition(
                name=tool_name,
                description=description or fn.__doc__ or "",
                input_schema=input_schema,
            )
            self.register(defn, fn, source="local")
            return fn
        return decorator

    def register_mcp_tools(
        self,
        server_name: str,
        tools: list[dict[str, Any]],
        executor: Callable[[str, dict[str, Any]], Coroutine[Any, Any, Any]],
    ) -> None:
        """批量注册 MCP 服务器的工具。

        Args:
            server_name: MCP 服务器名
            tools: MCP tools/list 返回的工具列表
            executor: 执行函数 (tool_name, arguments) -> result
        """
        for t in tools:
            tool_name = t.get("name", "")
            if not tool_name:
                continue
            defn = ToolDefinition(
                name=tool_name,
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {"type": "object", "properties": {}}),
            )
            # 闭包绑定 executor 和 tool_name
            async def _execute(args: dict[str, Any], _tn: str = tool_name, _ex=executor) -> Any:
                return await _ex(_tn, args)

            self.register(defn, _execute, source=f"mcp:{server_name}")

    # ─── 查询 ────────────────────────────────

    def get(self, name: str) -> ToolEntry | None:
        """获取工具条目。"""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_schemas(self, *, source_filter: str | None = None) -> list[dict[str, Any]]:
        """列出所有工具的 LLM function calling schema。

        Args:
            source_filter: 过滤来源（如 "local" 或 "mcp:server_name"）
        """
        schemas = []
        for entry in self._tools.values():
            if source_filter and not entry.source.startswith(source_filter):
                continue
            schemas.append(entry.definition.to_mcp_schema())
        return schemas

    def list_names(self) -> list[str]:
        """列出所有工具名。"""
        return list(self._tools.keys())

    # ─── 执行 ────────────────────────────────

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """执行工具。

        Args:
            name: 工具名
            arguments: 参数

        Returns:
            工具执行结果

        Raises:
            KeyError: 工具不存在
            RuntimeError: 工具无执行函数
        """
        entry = self._tools.get(name)
        if entry is None:
            raise KeyError(f"工具不存在: {name}")
        if entry.fn is None:
            raise RuntimeError(f"工具 '{name}' 无可执行函数（source={entry.source}）")

        logger.debug(f"执行工具: {name}({arguments})")
        return await entry.fn(**arguments)

    # ─── 管理 ────────────────────────────────

    def unregister(self, name: str) -> bool:
        """注销工具。"""
        return self._tools.pop(name, None) is not None

    def clear(self, *, source_filter: str | None = None) -> int:
        """清空工具。返回移除数量。"""
        if source_filter is None:
            count = len(self._tools)
            self._tools.clear()
            return count
        to_remove = [k for k, v in self._tools.items() if v.source.startswith(source_filter)]
        for k in to_remove:
            del self._tools[k]
        return len(to_remove)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ─────────────────────────────────────────────
# Schema 推断
# ─────────────────────────────────────────────


def _infer_schema(fn: Callable) -> dict[str, Any]:
    """从函数签名推断 JSON Schema。"""
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        annotation = param.annotation
        json_type = type_map.get(annotation, "string")
        properties[param_name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


# ─────────────────────────────────────────────
# 全局实例
# ─────────────────────────────────────────────

tool_registry = ToolRegistry()
