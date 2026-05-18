"""MCP 管理器测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.config.mcp_config import MCPConfig
from src.core.managers.tool_manager.mcp_adapter import MCPToolAdapter
from src.core.managers.tool_manager.mcp_manager import MCPManager, MCPServerMetadata


@pytest.mark.asyncio
async def test_mcp_manager_initialize_dispatches_all_server_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """initialize() 应覆盖 stdio / sse / streamable_http 三类服务。"""
    manager = MCPManager()
    config = MCPConfig(
        mcp=MCPConfig.MCPSection(
            enabled=True,
            stdio_servers={
                "fs": {"command": "npx", "args": ["-y", "server-filesystem"]},
            },
            sse_servers={
                "lab": {"url": "https://example.com/sse", "timeout": 10},
            },
            streamable_http_servers={
                "code": {"url": "https://example.com/mcp", "timeout": 30},
            },
        )
    )

    monkeypatch.setattr("src.core.config.get_mcp_config", lambda: config)

    manager.connect_stdio_server = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager.connect_sse_server_from_config = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager.connect_streamable_http_server_from_config = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _run_connection_task(_name: str, coro):
        return await coro

    manager._run_connection_task = AsyncMock(side_effect=_run_connection_task)  # type: ignore[method-assign]

    await manager.initialize()

    manager.connect_stdio_server.assert_awaited_once_with(
        "fs",
        "npx",
        ["-y", "server-filesystem"],
        None,
    )
    manager.connect_sse_server_from_config.assert_awaited_once_with(
        "lab",
        {"url": "https://example.com/sse", "timeout": 10},
    )
    manager.connect_streamable_http_server_from_config.assert_awaited_once_with(
        "code",
        {"url": "https://example.com/mcp", "timeout": 30},
    )


def test_mcp_manager_get_deferred_tool_classes_filters_by_metadata() -> None:
    """应仅返回配置为 defer_loading 的服务工具类。"""
    manager = MCPManager()

    class DeferredTool:
        pass

    class ImmediateTool:
        pass

    manager._sessions = {"deferred": object(), "immediate": object()}  # type: ignore[assignment]
    manager._server_metadata = {
        "deferred": MCPServerMetadata(
            server_name="deferred",
            instructions="",
            server_label="Deferred",
            defer_loading=True,
        ),
        "immediate": MCPServerMetadata(
            server_name="immediate",
            instructions="",
            server_label="Immediate",
            defer_loading=False,
        ),
    }
    manager._tool_classes_by_server = {
        "deferred": [DeferredTool],
        "immediate": [ImmediateTool],
    }

    assert manager.get_deferred_tool_classes() == [DeferredTool]


@pytest.mark.asyncio
async def test_mcp_tool_adapter_uses_bound_manager_and_normalizes_name() -> None:
    """适配器应复用绑定 manager，并将工具名规范化。"""
    fake_tool = SimpleNamespace(
        name="read_file",
        description="Read file",
        inputSchema={"type": "object"},
    )
    fake_manager = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(content=[], isError=False))
    )

    adapter = MCPToolAdapter("file_system", fake_tool, fake_manager)
    result = await adapter.execute({"path": "/tmp/demo.txt"})

    assert adapter.tool_name == "mcp-file-system-read-file"
    fake_manager.call_tool.assert_awaited_once_with(
        server_name="file_system",
        tool_name="read_file",
        arguments={"path": "/tmp/demo.txt"},
    )
    assert result["tool_name"] == "mcp-file-system-read-file"
