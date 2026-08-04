"""Bot 生命周期故障回滚测试。"""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.app.runtime.bot import Bot
from src.app.runtime.exceptions import BotShutdownError


def test_production_bootstrap_does_not_load_legacy_model_registry() -> None:
    """The old migration schema must not re-enter the production startup path."""

    source = inspect.getsource(Bot._initialize_kernel)

    assert "init_model_config" not in source
    assert "config/model.toml" not in source
    assert 'init_models_config("config/models.toml")' in source


def test_dns_warmup_uses_only_active_snapshot_providers() -> None:
    providers = {
        "active-http": {"base_url": "http://127.0.0.1:3000/v1"},
        "active-https": {"base_url": "https://gateway.example/v1"},
        "unused": {"base_url": "https://unused.example/v1"},
    }

    assert Bot._extract_active_provider_hosts(
        providers,
        ("active-http", "active-https"),
    ) == [("127.0.0.1", 3000), ("gateway.example", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_type", "expected_headers"),
    [
        ("openai", {"Authorization": "Bearer active-secret"}),
        (
            "anthropic",
            {
                "x-api-key": "active-secret",
                "anthropic-version": "2023-06-01",
            },
        ),
    ],
)
async def test_llm_preflight_uses_only_active_snapshot_providers(
    monkeypatch: pytest.MonkeyPatch,
    client_type: str,
    expected_headers: dict[str, str],
) -> None:
    """Startup health checks must follow the authoritative task snapshot."""

    bot = Bot()
    bot.config = SimpleNamespace(
        bot=SimpleNamespace(
            llm_preflight_check=True,
            llm_preflight_timeout=2.0,
        )
    )
    info_logs: list[str] = []
    bot.logger = SimpleNamespace(
        info=lambda message: info_logs.append(str(message)),
        warning=lambda *_: None,
        debug=lambda *_: None,
    )
    registry = SimpleNamespace(
        snapshot=SimpleNamespace(active_providers=("active",)),
        providers={
            "active": {
                "base_url": "http://active.example/v1",
                "api_key": "active-secret",
                "client_type": client_type,
            },
            "unused": {
                "base_url": "http://unused.example/v1",
                "api_key": "unused-secret",
            },
        },
    )
    monkeypatch.setattr(
        "src.kernel.config.models_loader.get_models_config",
        lambda: registry,
    )
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, *, headers: dict[str, str]):
            calls.append((url, headers))
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda **_kwargs: FakeClient())

    await bot._preflight_llm_providers()

    assert calls == [
        (
            "http://active.example/v1/models",
            expected_headers,
        )
    ]
    assert any("active OK" in message for message in info_logs)
    assert all("secret" not in message for message in info_logs)


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
        "_extract_active_provider_hosts",
        lambda _providers, _active: [],
    )
    monkeypatch.setattr(
        "src.kernel.config.models_loader.get_models_config",
        lambda: SimpleNamespace(
            providers={},
            snapshot=SimpleNamespace(active_providers=()),
        ),
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
    app_api_mount = SimpleNamespace(aclose=AsyncMock())
    bot.app_api_mount = app_api_mount
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
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
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
    app_api_mount.aclose.assert_awaited_once()
    assert bot.app_api_mount is None


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
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
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
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
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
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
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
