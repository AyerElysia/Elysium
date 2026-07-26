"""Bot 生命周期中的 MCP 集成测试。"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.runtime.bot import Bot


def _make_bot_for_runtime_mcp() -> Bot:
    """构造最小可测试 Bot 实例。"""
    bot = Bot.__new__(Bot)
    bot.ui = MagicMock()
    bot.logger = MagicMock()
    bot.config = MagicMock()
    bot.config.http_router.enable_http_router = False
    bot.event_bus = MagicMock()
    bot.event_bus.publish = AsyncMock(return_value=None)
    bot._unload_all_plugins = AsyncMock(return_value=None)  # type: ignore[method-assign]
    bot.scheduler = None
    bot.http_server = None
    bot.watchdog = None
    bot.task_manager = None
    bot.mcp_manager = None
    bot._stats = {}
    bot._shutdown_requested = False
    bot._running = True
    return bot


async def test_initialize_core_initializes_mcp_manager() -> None:
    """_initialize_core() 应初始化 MCP manager。"""
    bot = _make_bot_for_runtime_mcp()
    fake_mcp_manager = MagicMock()
    fake_mcp_manager.initialize = AsyncMock(return_value=None)

    with patch("src.core.transport.MessageReceiver", return_value=MagicMock()), patch(
        "src.core.transport.SinkManager",
        return_value=MagicMock(),
    ), patch("src.core.transport.sink.set_sink_manager"), patch(
        "src.core.managers.initialize_adapter_manager"
    ), patch("src.core.managers.initialize_router_manager"), patch(
        "src.core.managers.initialize_event_manager"
    ), patch("src.core.managers.initialize_distribution"), patch(
        "src.core.managers.get_mcp_manager",
        return_value=fake_mcp_manager,
    ):
        await bot._initialize_core()

    assert bot.mcp_manager is fake_mcp_manager
    fake_mcp_manager.initialize.assert_awaited_once()


async def test_shutdown_cleans_up_mcp_manager() -> None:
    """shutdown() 应关闭 MCP 客户端连接。"""
    bot = _make_bot_for_runtime_mcp()
    bot.mcp_manager = MagicMock()
    bot.mcp_manager.cleanup = AsyncMock(return_value=None)

    db_module = ModuleType("src.kernel.db")
    db_module.close_engine = AsyncMock(return_value=None)  # type: ignore[attr-defined]

    vector_db_module = ModuleType("src.kernel.vector_db")
    vector_db_module.close_all_vector_db_services = AsyncMock(return_value=None)  # type: ignore[attr-defined]

    logger_module = ModuleType("src.kernel.logger")
    logger_module.shutdown_logger_system = MagicMock()  # type: ignore[attr-defined]

    with patch.dict(
        sys.modules,
        {
            "src.kernel.db": db_module,
            "src.kernel.vector_db": vector_db_module,
            "src.kernel.logger": logger_module,
        },
    ):
        await bot.shutdown()

    bot.mcp_manager.cleanup.assert_awaited_once()
