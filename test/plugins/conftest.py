"""plugins 层测试 fixtures。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_life_engine_plugin(tmp_path: Path):
    """模拟 life_engine 插件实例。"""
    from plugins.life_engine.core.config import LifeEngineConfig

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return SimpleNamespace(config=config, logger=MagicMock())
