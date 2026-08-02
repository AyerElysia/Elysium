from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.components.base.config import BaseConfig
from src.core.components.base.agent import BaseAgent
from src.core.components.base.plugin import BasePlugin
from src.core.components.registry import get_global_registry
from src.core.components.state_manager import get_global_state_manager
from src.core.components.types import ComponentType
from src.core.components.loader import PluginManifest
from src.core.managers.plugin_manager import PluginManager


class _TestConfig(BaseConfig):
    config_name = "test_config"


@dataclass
class _FakeConfigInstance:
    value: str = "ok"


async def test_load_plugin_uses_class_configs_before_instantiation(monkeypatch) -> None:
    """插件应在实例化前通过 class configs 加载配置。"""

    class ConfigFirstPlugin(BasePlugin):
        plugin_name = "config_first_plugin"
        configs = [_TestConfig]

        def __init__(self, config=None) -> None:
            super().__init__(config)
            self.init_has_config = config is not None

        def get_components(self) -> list[type]:
            return []

    manager = PluginManager()
    fake_manifest = PluginManifest(
        name="config_first_plugin",
        version="1.0.0",
        description="test",
        author="test",
    )

    monkeypatch.setattr(manager, "_load_from_folder", AsyncMock(return_value=object()))

    import src.core.components.loader as loader_module

    monkeypatch.setattr(loader_module, "get_plugin_class", lambda _name: ConfigFirstPlugin)

    class _FakeConfigManager:
        def load_config(self, plugin_name: str, config_class: type[BaseConfig]):
            assert plugin_name == "config_first_plugin"
            assert config_class is _TestConfig
            return _FakeConfigInstance()

    import src.core.managers.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module,
        "get_config_manager",
        lambda: _FakeConfigManager(),
    )

    manager._register_components = AsyncMock()  # type: ignore[method-assign]

    success = await manager.load_plugin_from_manifest("fake/path", fake_manifest)

    assert success is True
    loaded = manager.get_plugin("config_first_plugin")
    assert loaded is not None
    assert getattr(loaded, "init_has_config") is True
    assert loaded.config is not None


async def test_register_components_includes_configs_class_property() -> None:
    """即使 get_components 未返回 Config，configs 里的配置类也应被注册。"""

    class RegisterConfigPlugin(BasePlugin):
        plugin_name = "register_config_plugin"
        configs = [_TestConfig]

        def get_components(self) -> list[type]:
            return []

    registry = get_global_registry()
    state_manager = get_global_state_manager()
    registry.clear()
    state_manager.clear()

    manager = PluginManager()
    plugin = RegisterConfigPlugin(config=None)

    await manager._register_components(plugin)

    config_components = registry.get_by_type(ComponentType.CONFIG)
    assert "register_config_plugin:config:test_config" in config_components
    assert config_components["register_config_plugin:config:test_config"] is _TestConfig


async def test_load_plugin_does_not_fallback_to_get_components_config(
    monkeypatch,
) -> None:
    """未声明 class configs 时，不应从 get_components 回退加载配置。"""

    class LegacyConfigInComponentsPlugin(BasePlugin):
        plugin_name = "legacy_config_plugin"

        def __init__(self, config=None) -> None:
            super().__init__(config)

        def get_components(self) -> list[type]:
            return [_TestConfig]

    manager = PluginManager()
    fake_manifest = PluginManifest(
        name="legacy_config_plugin",
        version="1.0.0",
        description="test",
        author="test",
    )

    monkeypatch.setattr(manager, "_load_from_folder", AsyncMock(return_value=object()))

    import src.core.components.loader as loader_module

    monkeypatch.setattr(
        loader_module,
        "get_plugin_class",
        lambda _name: LegacyConfigInComponentsPlugin,
    )

    class _FakeConfigManager:
        def load_config(self, plugin_name: str, config_class: type[BaseConfig]):
            raise AssertionError("不应从 get_components() 回退加载配置")

    import src.core.managers.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module,
        "get_config_manager",
        lambda: _FakeConfigManager(),
    )

    manager._register_components = AsyncMock()  # type: ignore[method-assign]

    success = await manager.load_plugin_from_manifest("fake/path", fake_manifest)

    assert success is True
    loaded = manager.get_plugin("legacy_config_plugin")
    assert loaded is not None
    assert loaded.config is None


async def test_load_hook_failure_rolls_back_all_plugin_state(monkeypatch) -> None:
    """启动钩子失败的插件不能以成功或半加载状态留在框架中。"""

    class FailingStartupPlugin(BasePlugin):
        plugin_name = "failing_startup_plugin"
        unloaded_calls = 0

        def get_components(self) -> list[type]:
            return []

        async def on_plugin_loaded(self) -> None:
            raise RuntimeError("startup failed")

        async def on_plugin_unloaded(self) -> None:
            type(self).unloaded_calls += 1

    manager = PluginManager()
    manifest = PluginManifest(
        name="failing_startup_plugin",
        version="1.0.0",
        description="test",
        author="test",
    )
    monkeypatch.setattr(manager, "_load_from_folder", AsyncMock(return_value=object()))
    monkeypatch.setattr(manager, "_register_components", AsyncMock())
    monkeypatch.setattr(manager, "_unregister_plugin_components", AsyncMock())
    monkeypatch.setattr(manager, "_cleanup_sys_modules", MagicMock())
    monkeypatch.setattr(manager, "_cleanup_plugin_import_paths", MagicMock())
    monkeypatch.setattr(
        "src.core.components.loader.get_plugin_class",
        lambda _name: FailingStartupPlugin,
    )
    unregister_plugin = MagicMock()
    monkeypatch.setattr(
        "src.core.components.loader.unregister_plugin",
        unregister_plugin,
    )
    config_manager = MagicMock()
    monkeypatch.setattr(
        "src.core.managers.config_manager.get_config_manager",
        lambda: config_manager,
    )
    event_manager = MagicMock(unregister_plugin_handlers=AsyncMock())
    monkeypatch.setattr(
        "src.core.managers.event_manager.get_event_manager",
        lambda: event_manager,
    )

    success = await manager.load_plugin_from_manifest("fake/path", manifest)

    assert success is False
    assert manager.get_plugin("failing_startup_plugin") is None
    assert FailingStartupPlugin.unloaded_calls == 1
    manager._unregister_plugin_components.assert_awaited_once_with(
        "failing_startup_plugin"
    )
    event_manager.unregister_plugin_handlers.assert_awaited_once_with(
        "failing_startup_plugin"
    )
    config_manager.remove_config.assert_called_once_with("failing_startup_plugin")
    unregister_plugin.assert_called_once_with("failing_startup_plugin")


async def test_register_components_ignores_config_from_get_components() -> None:
    """get_components 返回的 Config 组件应被忽略，不应注册。"""

    class IgnoreLegacyConfigPlugin(BasePlugin):
        plugin_name = "ignore_legacy_config_plugin"

        def get_components(self) -> list[type]:
            return [_TestConfig]

    registry = get_global_registry()
    state_manager = get_global_state_manager()
    registry.clear()
    state_manager.clear()

    manager = PluginManager()
    plugin = IgnoreLegacyConfigPlugin(config=None)

    await manager._register_components(plugin)

    config_components = registry.get_by_type(ComponentType.CONFIG)
    assert "ignore_legacy_config_plugin:config:test_config" not in config_components


async def test_register_components_supports_agent_type() -> None:
    """插件组件注册应支持 Agent 组件类型。"""

    class _TestAgent(BaseAgent):
        agent_name = "planner"
        agent_description = "planner agent"

        async def execute(self, task: str) -> tuple[bool, str]:
            return True, task

    class AgentPlugin(BasePlugin):
        plugin_name = "agent_plugin"

        def get_components(self) -> list[type]:
            return [_TestAgent]

    registry = get_global_registry()
    state_manager = get_global_state_manager()
    registry.clear()
    state_manager.clear()

    manager = PluginManager()
    plugin = AgentPlugin(config=None)

    await manager._register_components(plugin)

    agent_components = registry.get_by_type(ComponentType.AGENT)
    assert "agent_plugin:agent:planner" in agent_components
    assert agent_components["agent_plugin:agent:planner"] is _TestAgent


def _prepare_loaded_plugin_for_unload(
    manager: PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    plugin_name: str,
) -> None:
    manager._loaded_plugins[plugin_name] = MagicMock(
        on_plugin_unloaded=AsyncMock()
    )
    manager._plugin_paths[plugin_name] = f"plugins/{plugin_name}"
    monkeypatch.setattr(
        "src.kernel.event.get_event_bus",
        lambda: MagicMock(publish=AsyncMock()),
    )
    monkeypatch.setattr(
        "src.core.components.state_manager.get_global_state_manager",
        lambda: MagicMock(set_state_async=AsyncMock()),
    )
    monkeypatch.setattr(
        "src.core.managers.event_manager.get_event_manager",
        lambda: MagicMock(unregister_plugin_handlers=AsyncMock()),
    )
    monkeypatch.setattr("src.core.components.loader.unregister_plugin", MagicMock())
    monkeypatch.setattr(manager, "_unregister_plugin_components", AsyncMock())
    monkeypatch.setattr(manager, "_cleanup_sys_modules", MagicMock())


async def test_reload_plugin_orders_unload_cache_removal_and_load(monkeypatch) -> None:
    """重载顺序应为卸载、清除配置缓存、重新加载，且缓存只清一次。"""
    operations: list[str] = []
    remove_config = MagicMock(
        side_effect=lambda plugin_name: operations.append("remove_config")
    )
    config_manager = MagicMock(remove_config=remove_config)
    manager = PluginManager()
    _prepare_loaded_plugin_for_unload(manager, monkeypatch, "reload_plugin")
    original_unload = manager._unload_plugin_unlocked
    manifest = PluginManifest(
        name="reload_plugin",
        version="1.0.0",
        description="test",
        author="test",
    )

    async def tracked_unload(plugin_name: str) -> bool:
        operations.append("unload")
        return await original_unload(plugin_name)

    async def tracked_load(plugin_path: str, loaded_manifest: PluginManifest) -> bool:
        assert loaded_manifest is manifest
        operations.append("load")
        return True

    monkeypatch.setattr(
        manager,
        "_unload_plugin_unlocked",
        AsyncMock(side_effect=tracked_unload),
    )
    monkeypatch.setattr(
        manager,
        "_load_plugin_from_manifest_unlocked",
        AsyncMock(side_effect=tracked_load),
    )
    monkeypatch.setattr(
        "src.core.components.loader.load_manifest",
        AsyncMock(return_value=manifest),
    )
    monkeypatch.setattr(
        "src.core.managers.config_manager.get_config_manager",
        lambda: config_manager,
    )

    result = await manager.reload_plugin("reload_plugin")

    assert result is True
    assert operations == ["unload", "remove_config", "load"]
    remove_config.assert_called_once_with("reload_plugin")
    manager._unload_plugin_unlocked.assert_awaited_once_with("reload_plugin")
    manager._load_plugin_from_manifest_unlocked.assert_awaited_once_with(
        "plugins/reload_plugin",
        manifest,
    )


async def test_unload_plugin_removes_config_cache_on_success(monkeypatch) -> None:
    """直接成功卸载插件时应清除该插件的配置缓存。"""
    config_manager = MagicMock()
    manager = PluginManager()
    _prepare_loaded_plugin_for_unload(manager, monkeypatch, "cached_plugin")
    monkeypatch.setattr(
        "src.core.managers.config_manager.get_config_manager",
        lambda: config_manager,
    )

    result = await manager.unload_plugin("cached_plugin")

    assert result is True
    config_manager.remove_config.assert_called_once_with("cached_plugin")


async def test_unload_plugin_still_clears_cache_after_partial_failure(
    monkeypatch,
) -> None:
    """卸载局部失败时仍应释放其余资源，避免留下半卸载状态。"""
    config_manager = MagicMock()
    manager = PluginManager()
    _prepare_loaded_plugin_for_unload(manager, monkeypatch, "failing_plugin")
    monkeypatch.setattr(
        manager,
        "_unregister_plugin_components",
        AsyncMock(side_effect=RuntimeError("unregister failed")),
    )
    monkeypatch.setattr(
        "src.core.managers.config_manager.get_config_manager",
        lambda: config_manager,
    )

    result = await manager.unload_plugin("failing_plugin")

    assert result is False
    config_manager.remove_config.assert_called_once_with("failing_plugin")
    assert "failing_plugin" not in manager._loaded_plugins
