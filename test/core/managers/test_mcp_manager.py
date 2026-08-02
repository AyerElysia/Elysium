"""MCP 管理器测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.config.mcp_config import MCPConfig
from src.core.managers.tool_manager.mcp_adapter import MCPToolAdapter
from src.core.managers.tool_manager.mcp_manager import MCPManager, MCPServerMetadata


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


async def test_mcp_manager_connects_configured_servers_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One slow MCP server must not add its full timeout to every later server."""

    manager = MCPManager()
    config = MCPConfig(
        mcp=MCPConfig.MCPSection(
            enabled=True,
            stdio_servers={
                "first": {"command": "one"},
                "second": {"command": "two"},
            },
        )
    )
    monkeypatch.setattr("src.core.config.get_mcp_config", lambda: config)
    active = 0
    maximum_active = 0

    async def connect(*_args, **_kwargs) -> bool:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.02)
            return True
        finally:
            active -= 1

    async def run_connection(_name: str, coro):
        return await coro

    manager.connect_stdio_server = connect  # type: ignore[method-assign]
    manager._run_connection_task = run_connection  # type: ignore[method-assign]

    await manager.initialize()

    assert maximum_active == 2


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


async def test_failed_mcp_connection_closes_partial_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接初始化失败时必须立即关闭该服务已打开的传输。"""
    manager = MCPManager()
    transport_closed = asyncio.Event()

    class FakeTransportContext:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *_args):
            transport_closed.set()

    monkeypatch.setattr(
        "src.core.managers.tool_manager.mcp_manager.stdio_client",
        lambda _params: FakeTransportContext(),
    )
    manager._connect_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("initialize failed")
    )

    success = await manager.connect_stdio_server(
        "broken",
        "fake-command",
        [],
    )

    assert success is False
    assert transport_closed.is_set()
    assert "broken" not in manager._sessions


async def test_mcp_tool_call_has_hard_timeout() -> None:
    """失联 MCP 工具不能无限占住调用链。"""
    manager = MCPManager()
    never_returns = asyncio.Event()

    async def _hang(*_args):
        await never_returns.wait()

    manager._sessions["slow"] = SimpleNamespace(
        call_tool=AsyncMock(side_effect=_hang)
    )

    with pytest.raises(asyncio.TimeoutError):
        await manager.call_tool("slow", "hang", {}, timeout=0.01)


async def test_mcp_cleanup_has_a_hard_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken transport exit must not hold application shutdown forever."""

    manager = MCPManager()
    never_returns = asyncio.Event()

    async def hang_close() -> None:
        await never_returns.wait()

    manager._exit_stack = SimpleNamespace(aclose=hang_close)  # type: ignore[assignment]
    monkeypatch.setattr(
        "src.core.managers.tool_manager.mcp_manager._MCP_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )

    await asyncio.wait_for(manager.cleanup(), timeout=0.1)

    assert manager._sessions == {}
