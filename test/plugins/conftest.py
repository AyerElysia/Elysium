"""plugins 层测试 fixtures。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_life_engine_plugin(tmp_path: Path):
    """模拟 life_engine 插件实例。"""
    from plugins.life_engine.core.config import LifeEngineConfig

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return SimpleNamespace(config=config, logger=MagicMock())


@pytest.fixture
def synthetic_chatter_authority(monkeypatch: pytest.MonkeyPatch):
    """Provide explicit test-only authority while retaining the prefix guard."""
    from plugins.life_engine.core.chatter import LifeChatter

    monkeypatch.setattr(
        LifeChatter, "_load_subject_authority_texts",
        AsyncMock(return_value={"SOUL.md": "Synthetic engineering fixture."}),
    )
    monkeypatch.setattr(LifeChatter, "_load_workspace_markdown", lambda *args: "")


@pytest.fixture
def registered_minecraft_extension(monkeypatch: pytest.MonkeyPatch):
    """Load only the real plugin declaration; never start a game or service."""
    from plugins.life_engine.service import scene_extensions
    from plugins.minecraft.service import MINECRAFT_EXTENSION

    monkeypatch.setattr(scene_extensions, "_extensions", {})
    owner = object()
    scene_extensions.register_scene_extension(owner, MINECRAFT_EXTENSION)
    try:
        yield
    finally:
        scene_extensions.unregister_scene_extension(owner)
