"""Independent plugin ownership, disabled state, rollback and capability contracts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.scene_extensions import (
    SceneExtension, get_scene_extension, register_scene_extension,
    requires_result_before_reply, unregister_scene_extension,
)
from plugins.life_engine.service.tool_manifests import get_tool_manifest
from plugins.minecraft.config import MinecraftConfig
from plugins.minecraft.plugin import MinecraftPlugin
from plugins.minecraft.service import MinecraftService
import plugins.minecraft.service as service_module


@pytest.fixture
def enabled_plugin():
    plugin = MinecraftPlugin(MinecraftConfig.model_validate({"settings": {"enabled": True}}))
    yield plugin
    unregister_scene_extension(plugin.service)
    if MinecraftService._instance is plugin.service:
        MinecraftService._instance = None


def test_life_engine_contains_no_game_configuration_or_components():
    assert "minecraft" not in LifeEngineConfig.model_fields
    assert not hasattr(LifeEngineService, "minecraft_session")
    assert all(getattr(cls, "tool_name", "") != "nucleus_minecraft"
               for cls in LifeEnginePlugin(LifeEngineConfig()).get_components())


def test_plugin_manifest_and_component_ownership(enabled_plugin):
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "plugins/minecraft/manifest.json").read_text())
    assert manifest["name"] == "minecraft"
    assert manifest["dependencies"]["plugins"] == ["life_engine"]
    assert manifest["entry_point"] == "plugin.py"
    assert {item["component_name"] for item in manifest["include"]} == {"minecraft", "nucleus_minecraft"}
    assert len(enabled_plugin.get_components()) == 2
    assert MinecraftService(enabled_plugin) is enabled_plugin.service


async def test_disabled_plugin_has_no_session_or_scene_capabilities(monkeypatch):
    plugin = MinecraftPlugin()
    monkeypatch.setattr(service_module, "get_life_engine_service", lambda: pytest.fail("disabled plugin reads no runtime"))
    assert plugin.get_components() == []
    await plugin.on_plugin_loaded()
    assert plugin.service.session is None
    assert get_scene_extension("minecraft") is None
    assert "tool-nucleus_minecraft" not in get_tool_manifest("chat")
    await plugin.on_plugin_unloaded()


async def test_duplicate_start_and_stop_have_one_owner(enabled_plugin, monkeypatch):
    fake_life = object()
    monkeypatch.setattr(service_module, "get_life_engine_service", lambda: fake_life)
    session = SimpleNamespace(close=AsyncMock(return_value={"success": True}))
    calls = []
    def create(life):
        assert life is fake_life
        calls.append(life)
        return session
    monkeypatch.setattr(enabled_plugin.service, "_create_session", create)
    enabled_plugin.config.settings.evidence_max_result_bytes = 4096
    await asyncio.gather(*(enabled_plugin.on_plugin_loaded() for _ in range(8)))
    assert len(calls) == 1
    assert MinecraftService.get_instance() is enabled_plugin.service
    assert get_scene_extension("minecraft").evidence_budget_bytes == 4096
    assert "tool-nucleus_minecraft" in get_tool_manifest("chat")
    assert requires_result_before_reply("nucleus_minecraft")
    scene_tools = set(get_tool_manifest("minecraft"))
    assert {"tool-nucleus_minecraft", "tool-nucleus_proactive_query",
            "tool-nucleus_proactive_command"} <= scene_tools
    assert all(name.startswith(("tool-", "action-")) for name in scene_tools)
    assert "tool-nucleus_schedule_autonomy_intent" not in scene_tools
    await asyncio.gather(*(enabled_plugin.on_plugin_unloaded() for _ in range(8)))
    session.close.assert_awaited_once()
    assert MinecraftService.get_instance() is None
    assert not requires_result_before_reply("nucleus_minecraft")
    with pytest.raises(KeyError):
        get_tool_manifest("minecraft")


async def test_creation_failure_rolls_back_capabilities(enabled_plugin, monkeypatch):
    monkeypatch.setattr(service_module, "get_life_engine_service", lambda: object())
    def fail(_life):
        raise RuntimeError("injected construction failure")
    monkeypatch.setattr(enabled_plugin.service, "_create_session", fail)
    with pytest.raises(RuntimeError, match="injected construction"):
        await enabled_plugin.on_plugin_loaded()
    assert get_scene_extension("minecraft") is None
    assert enabled_plugin.service.session is None
    assert enabled_plugin.service._life is None


async def test_missing_life_engine_fails_without_acquiring_session(enabled_plugin, monkeypatch):
    monkeypatch.setattr(service_module, "get_life_engine_service", lambda: None)
    with pytest.raises(RuntimeError, match="RequiresRunningLifeEngine"):
        await enabled_plugin.on_plugin_loaded()
    assert enabled_plugin.service.session is None
    assert get_scene_extension("minecraft") is None


async def test_close_failure_retains_handle_for_owned_retry(enabled_plugin, monkeypatch):
    monkeypatch.setattr(service_module, "get_life_engine_service", lambda: object())
    session = SimpleNamespace(close=AsyncMock(side_effect=[RuntimeError("close failed"), {"success": True}]))
    monkeypatch.setattr(enabled_plugin.service, "_create_session", lambda _life: session)
    await enabled_plugin.on_plugin_loaded()
    with pytest.raises(RuntimeError, match="close failed"):
        await enabled_plugin.on_plugin_unloaded()
    assert enabled_plugin.service.session is session
    assert MinecraftService.get_instance() is enabled_plugin.service
    await enabled_plugin.on_plugin_unloaded()
    assert enabled_plugin.service.session is None


def test_scene_extension_rejects_other_owner_and_cannot_overwrite_core_manifest():
    owner, intruder = object(), object()
    extension = SceneExtension(kind="test_scene", tools=("tool-scene",), chat_tools=("tool-scene",))
    try:
        register_scene_extension(owner, extension)
        register_scene_extension(owner, extension)
        with pytest.raises(RuntimeError, match="AlreadyRegistered"):
            register_scene_extension(intruder, extension)
        unregister_scene_extension(intruder)
        assert get_tool_manifest("test_scene") == ["tool-scene"]
        with pytest.raises(ValueError, match="KindConflict"):
            register_scene_extension(intruder, SceneExtension("chat", ()))
    finally:
        unregister_scene_extension(owner)


def test_session_uses_shared_identity_and_event_ports(enabled_plugin, tmp_path):
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    life = LifeEngineService(SimpleNamespace(config=config))
    scene = enabled_plugin.service
    scene._life = life
    session = scene._create_session(life)
    assert session._get_recent_subconscious_context.__self__ is life
    assert session._get_subject_context_projection_snapshot.__self__ is life
    assert session._record_minecraft_body_event.__self__ is scene
    assert session._record_minecraft_consciousness_decision.__self__ is scene
    assert session._record_conscious_model_turn.__self__ is life


def test_life_engine_source_has_no_game_imports_or_special_cases():
    directory = Path(__file__).resolve().parents[3] / "plugins/life_engine"
    for path in directory.rglob("*.py"):
        assert "minecraft" not in path.read_text(encoding="utf-8").lower(), path


def test_windows_helpers_are_packaged_with_independent_plugin():
    directory = Path(__file__).resolve().parents[3] / "plugins/minecraft"
    for filename in ("win_helper.ps1", "win_launch.ps1"):
        assert (directory / filename).is_file()
        assert (directory / filename).stat().st_size > 100


async def test_manifest_dependency_order_places_game_after_shared_runtime():
    from src.core.components.loader import PluginDependencyResolver, load_manifest

    root = Path(__file__).resolve().parents[3]
    life = await load_manifest(str(root / "plugins/life_engine"))
    game = await load_manifest(str(root / "plugins/minecraft"))
    assert life is not None and game is not None
    resolver = PluginDependencyResolver()
    resolver.add_plugin(game)
    resolver.add_plugin(life)
    assert resolver.resolve_load_order() == ["life_engine", "minecraft"]
