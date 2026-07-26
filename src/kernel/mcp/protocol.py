"""MCP & 工具统一协议。

定义工具的标准接口，与 MCP spec 对齐。
本地工具和 MCP 远程工具共享同一协议，调用方无感知差异。

协议对齐：
- name: 工具唯一标识
- description: 自然语言描述
- inputSchema: JSON Schema（与 MCP tools/list 返回格式一致）
- execute(): 执行并返回结果
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass
class ToolDefinition:
    """工具定义（与 MCP Tool schema 对齐）。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    annotations: dict[str, Any] = field(default_factory=dict)

    def to_mcp_schema(self) -> dict[str, Any]:
        """导出为 MCP/LLM function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_openai_schema(self) -> dict[str, Any]:
        """导出为 OpenAI tools 格式。"""
        return self.to_mcp_schema()


# 工具执行函数签名
ToolFn = Callable[..., Coroutine[Any, Any, Any]]


@dataclass
class ToolEntry:
    """注册表中的工具条目。"""

    definition: ToolDefinition
    fn: ToolFn | None = None
    source: str = "local"  # "local" | "mcp:{server_name}"
    metadata: dict[str, Any] = field(default_factory=dict)
