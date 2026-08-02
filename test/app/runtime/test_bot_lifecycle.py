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
    shutdown_order = []
    bot._unload_all_plugins = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: shutdown_order.append("plugins")
    )
    network_cleanup = Mock()
    bot._shutdown_async_network_runtime = network_cleanup  # type: ignore[method-assign]

    adapter_manager = SimpleNamespace(
        stop_all_adapters=AsyncMock(
            side_effect=lambda: shutdown_order.append("adapters") or {}
        ),
        list_active_adapters=Mock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: adapter_manager,
    )

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
    adapter_manager.stop_all_adapters.assert_awaited_once()
    bot._unload_all_plugins.assert_awaited_once()
    assert shutdown_order[:2] == ["adapters", "plugins"]
    stream_manager.stop.assert_awaited_once()
    close_engine.assert_awaited_once()
    close_vectors.assert_awaited_once()
    close_logger.assert_awaited_once()
    network_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_recovers_when_plugin_unload_finishes_adapter_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient first-pass adapter failure must not become a fatal error."""

    bot = Bot()
    signature = "napcat_adapter:adapter:napcat_adapter"
    active = [signature]
    adapter_manager = SimpleNamespace(
        stop_all_adapters=AsyncMock(return_value={signature: False}),
        list_active_adapters=Mock(side_effect=lambda: list(active)),
    )

    async def unload_plugins() -> None:
        active.clear()

    bot._unload_all_plugins = unload_plugins  # type: ignore[method-assign]
    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: adapter_manager,
    )
    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager."
        "get_stream_loop_manager",
        lambda: SimpleNamespace(stop=AsyncMock()),
    )
    monkeypatch.setattr("src.kernel.db.close_engine", AsyncMock())
    monkeypatch.setattr(
        "src.kernel.llm.model_client.close_default_model_clients",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "src.kernel.vector_db.close_all_vector_db_services",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "src.kernel.logger.shutdown_logger_system_async",
        AsyncMock(),
    )

    await bot.shutdown()

    adapter_manager.stop_all_adapters.assert_awaited_once()
    assert adapter_manager.list_active_adapters.call_count == 1


@pytest.mark.asyncio
async def test_shutdown_reports_adapter_only_after_retry_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truly live adapter after the retry remains a shutdown failure."""

    bot = Bot()
    signature = "napcat_adapter:adapter:napcat_adapter"
    adapter_manager = SimpleNamespace(
        stop_all_adapters=AsyncMock(return_value={signature: False}),
        list_active_adapters=Mock(return_value=[signature]),
    )
    bot._unload_all_plugins = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: adapter_manager,
    )
    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager."
        "get_stream_loop_manager",
        lambda: SimpleNamespace(stop=AsyncMock()),
    )
    monkeypatch.setattr("src.kernel.db.close_engine", AsyncMock())
    monkeypatch.setattr(
        "src.kernel.llm.model_client.close_default_model_clients",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "src.kernel.vector_db.close_all_vector_db_services",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "src.kernel.logger.shutdown_logger_system_async",
        AsyncMock(),
    )

    with pytest.raises(BotShutdownError, match="adapters_verify"):
        await bot.shutdown()

    assert adapter_manager.stop_all_adapters.await_count == 2


@pytest.mark.asyncio
async def test_shutdown_deadline_is_hard_even_when_cleanup_delays_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cancellation-resistant cleanup step must not hang shutdown forever."""

    bot = Bot()
    stream_manager = SimpleNamespace(stop=AsyncMock())
    adapter_manager = SimpleNamespace(stop_all_adapters=AsyncMock(return_value={}))
    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager."
        "get_stream_loop_manager",
        lambda: stream_manager,
    )
    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: adapter_manager,
    )

    async def delayed_cancellation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)

    bot._unload_all_plugins = delayed_cancellation  # type: ignore[method-assign]
    started_at = asyncio.get_running_loop().time()

    await bot.shutdown(timeout=0.03, raise_on_error=False)

    elapsed = asyncio.get_running_loop().time() - started_at
    assert elapsed < 0.15
    await asyncio.sleep(0.25)
