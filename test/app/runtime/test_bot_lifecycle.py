"""Bot 生命周期故障回滚测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.app.runtime.bot import Bot
from src.app.runtime.exceptions import BotShutdownError


@pytest.mark.asyncio
async def test_network_runtime_restores_loop_methods_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS 专用线程池不能永久修改宿主事件循环。"""
    bot = Bot()
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo
    original_getnameinfo = loop.getnameinfo
    monkeypatch.setattr(
        bot,
        "_extract_provider_hosts_from_model_config",
        lambda _path: [],
    )

    await bot._optimize_async_network_runtime()

    assert loop.getaddrinfo is not original_getaddrinfo
    assert loop.getnameinfo is not original_getnameinfo

    bot._shutdown_async_network_runtime()

    assert loop.getaddrinfo == original_getaddrinfo
    assert loop.getnameinfo == original_getnameinfo
    assert bot._dns_executor is None


@pytest.mark.asyncio
async def test_shutdown_continues_after_independent_step_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个组件关闭失败时，后续资源仍必须全部释放。"""
    bot = Bot()
    bot.event_bus = SimpleNamespace(
        publish=AsyncMock(side_effect=RuntimeError("event stop failed"))
    )
    bot.scheduler = SimpleNamespace(stop=AsyncMock())
    bot._unload_all_plugins = AsyncMock()  # type: ignore[method-assign]
    network_cleanup = Mock()
    bot._shutdown_async_network_runtime = network_cleanup  # type: ignore[method-assign]

    stream_manager = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager."
        "get_stream_loop_manager",
        lambda: stream_manager,
    )
    close_engine = AsyncMock()
    close_vectors = AsyncMock()
    close_logger = AsyncMock()
    monkeypatch.setattr("src.kernel.db.close_engine", close_engine)
    monkeypatch.setattr(
        "src.kernel.vector_db.close_all_vector_db_services",
        close_vectors,
    )
    monkeypatch.setattr(
        "src.kernel.logger.shutdown_logger_system_async",
        close_logger,
    )

    with pytest.raises(BotShutdownError, match="on_stop"):
        await bot.shutdown()

    bot.scheduler.stop.assert_awaited_once()
    bot._unload_all_plugins.assert_awaited_once()
    stream_manager.stop.assert_awaited_once()
    close_engine.assert_awaited_once()
    close_vectors.assert_awaited_once()
    close_logger.assert_awaited_once()
    network_cleanup.assert_called_once()
