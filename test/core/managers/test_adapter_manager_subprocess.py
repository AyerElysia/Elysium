import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock


class _DummySinkManager:
    """Track adapter sink ownership for lifecycle tests."""

    def __init__(self) -> None:
        self.sinks = {}
        self.setup_count = 0
        self.teardown_count = 0

    async def setup_adapter_sink(self, signature, adapter) -> None:
        self.setup_count += 1
        self.sinks[signature] = object()

    def get_sink(self, signature):
        return self.sinks.get(signature)

    async def teardown_adapter_sink(self, signature) -> None:
        self.teardown_count += 1
        self.sinks.pop(signature, None)


def _install_lifecycle_dependencies(monkeypatch, adapter_cls, sink_manager) -> None:
    """Install deterministic registry, plugin, state, and sink dependencies."""

    class DummyRegistry:
        def get(self, _signature):
            return adapter_cls

    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_global_registry",
        lambda: DummyRegistry(),
    )
    monkeypatch.setattr(
        "src.core.managers.adapter_manager._get_plugin_manager",
        lambda: SimpleNamespace(get_plugin=lambda _name: object()),
    )
    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_global_state_manager",
        lambda: SimpleNamespace(set_state_async=AsyncMock()),
    )
    monkeypatch.setattr(
        "src.core.transport.sink.sink_manager.get_sink_manager",
        lambda: sink_manager,
    )


async def test_adapter_manager_subprocess_mode_is_rejected(monkeypatch):
    """子进程适配器支持已移除：声明 run_in_subprocess=True 的适配器应被拒绝启动。"""

    from src.core.managers.adapter_manager import AdapterManager

    class DummyAdapter:
        run_in_subprocess = True
        platform = "dummy"

    class DummyRegistry:
        def get(self, sig):
            return DummyAdapter

    # 替换 registry（应在 run_in_subprocess 检测处直接返回，无需 state_manager 参与）
    monkeypatch.setattr("src.core.managers.adapter_manager.get_global_registry", lambda: DummyRegistry())

    manager = AdapterManager()
    ok = await manager.start_adapter("p:adapter:x")
    assert ok is False

    adapter = manager.get_adapter("p:adapter:x")
    assert adapter is None


async def test_on_all_plugins_loaded_schedules_background_start(monkeypatch):
    """所有适配器都应立即调度到后台启动并返回。"""

    from src.core.managers.adapter_manager import on_all_plugins_loaded
    from src.kernel.event import EventDecision

    class DummyAdapter:
        run_in_subprocess = False

    class DummyRegistry:
        def get_by_type(self, _component_type):
            return {"napcat:adapter:napcat_adapter": DummyAdapter}

    class DummyTaskManager:
        def __init__(self) -> None:
            self.tasks = []

        def create_task(
            self,
            coro,
            name=None,
            daemon=False,
            timeout=None,
            group_name=None,
            metadata=None,
        ):
            task = asyncio.create_task(coro, name=name)
            self.tasks.append(task)
            return type("TaskInfo", (), {"task": task, "task_id": "task-id"})()

    task_manager = DummyTaskManager()
    start_adapter = AsyncMock(return_value=True)
    mock_manager = SimpleNamespace(start_adapter=start_adapter)

    monkeypatch.setattr("src.core.managers.adapter_manager.get_global_registry", lambda: DummyRegistry())
    monkeypatch.setattr("src.core.managers.adapter_manager.get_task_manager", lambda: task_manager)
    monkeypatch.setattr("src.core.managers.adapter_manager.get_adapter_manager", lambda: mock_manager)

    decision, out = await on_all_plugins_loaded("", {})

    assert decision == EventDecision.SUCCESS
    assert out == {}
    assert len(task_manager.tasks) == 1

    await task_manager.tasks[0]

    start_adapter.assert_awaited_once_with("napcat:adapter:napcat_adapter")


async def test_concurrent_starts_create_only_one_adapter(monkeypatch):
    """Two startup requests for one signature must share one lifecycle."""

    from src.core.managers.adapter_manager import AdapterManager

    class DummyAdapter:
        run_in_subprocess = False
        platform = "dummy"
        instances = 0
        starts = 0

        def __init__(self, core_sink, plugin) -> None:
            type(self).instances += 1
            self.core_sink = core_sink
            self.plugin = plugin

        async def start(self) -> None:
            type(self).starts += 1
            await asyncio.sleep(0.01)

        async def stop(self) -> None:
            return None

    sink_manager = _DummySinkManager()
    _install_lifecycle_dependencies(monkeypatch, DummyAdapter, sink_manager)
    manager = AdapterManager()

    results = await asyncio.gather(
        manager.start_adapter("p:adapter:x"),
        manager.start_adapter("p:adapter:x"),
    )

    assert results == [True, True]
    assert DummyAdapter.instances == 1
    assert DummyAdapter.starts == 1
    assert sink_manager.setup_count == 1
    assert manager.list_active_adapters() == ["p:adapter:x"]


async def test_failed_start_rolls_back_adapter_and_sink(monkeypatch):
    """A partial startup must not retain a sink or active adapter entry."""

    from src.core.managers.adapter_manager import AdapterManager

    class FailingAdapter:
        run_in_subprocess = False
        platform = "dummy"
        stops = 0

        def __init__(self, core_sink, plugin) -> None:
            self.core_sink = core_sink

        async def start(self) -> None:
            raise OSError("bind failed")

        async def stop(self) -> None:
            type(self).stops += 1

    sink_manager = _DummySinkManager()
    _install_lifecycle_dependencies(monkeypatch, FailingAdapter, sink_manager)
    manager = AdapterManager()

    assert await manager.start_adapter("p:adapter:x") is False
    assert manager.get_adapter("p:adapter:x") is None
    assert sink_manager.sinks == {}
    assert sink_manager.teardown_count == 1
    assert FailingAdapter.stops == 1


async def test_successful_stop_tears_down_sink(monkeypatch):
    """Stopping an adapter must release its message sink as well."""

    from src.core.managers.adapter_manager import AdapterManager

    class DummyAdapter:
        run_in_subprocess = False
        platform = "dummy"

        def __init__(self, core_sink, plugin) -> None:
            self.core_sink = core_sink

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    sink_manager = _DummySinkManager()
    _install_lifecycle_dependencies(monkeypatch, DummyAdapter, sink_manager)
    manager = AdapterManager()

    assert await manager.start_adapter("p:adapter:x") is True
    assert await manager.stop_adapter("p:adapter:x") is True
    assert sink_manager.sinks == {}
    assert sink_manager.teardown_count == 1
