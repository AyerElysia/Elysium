"""MCP & 工具统一层。

kernel 级工具基础设施：
- protocol.py: 工具协议定义（对齐 MCP spec）
- registry.py: 统一注册表（本地 + MCP 远程）

用法：
    from src.kernel.mcp import tool_registry, ToolDefinition
"""

from .protocol import ToolDefinition, ToolEntry, ToolFn
from .registry import ToolRegistry, tool_registry

__all__ = [
    "ToolDefinition",
    "ToolEntry",
    "ToolFn",
    "ToolRegistry",
    "tool_registry",
]
