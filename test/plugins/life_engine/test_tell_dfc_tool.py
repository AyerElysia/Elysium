"""The DFC wake adapter class is gone; the name is not kept as a stub."""

from __future__ import annotations

import inspect

from plugins.life_engine.agents.registry import _UNIVERSAL_DISALLOW
from plugins.life_engine.agents.worker import _FORBIDDEN_TOOL_NAMES
from plugins.life_engine.core.chatter import _RETIRED_PROACTIVE_ACTIONS
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.tools import ALL_TOOLS, file_tools


def test_tell_dfc_class_is_removed() -> None:
    assert not hasattr(file_tools, "LifeEngineWakeDFCTool")
    assert "nucleus_tell_dfc" not in {tool.tool_name for tool in ALL_TOOLS}


def test_tell_dfc_is_not_registered_for_runtime_use() -> None:
    plugin = LifeEnginePlugin(config=LifeEngineConfig())
    names: set[str] = set()
    for component in plugin.get_components():
        for attr in ("tool_name", "action_name", "__name__"):
            value = getattr(component, attr, None)
            if value:
                names.add(str(value))
    assert "nucleus_tell_dfc" not in names
    assert "LifeEngineWakeDFCTool" not in names


def test_tell_dfc_name_has_no_placeholder_slot() -> None:
    assert "nucleus_tell_dfc" not in _RETIRED_PROACTIVE_ACTIONS
    assert "tool-nucleus_tell_dfc" not in _RETIRED_PROACTIVE_ACTIONS
    assert "nucleus_tell_dfc" not in _UNIVERSAL_DISALLOW
    assert "nucleus_tell_dfc" not in _FORBIDDEN_TOOL_NAMES
    source = inspect.getsource(LifeEngineService._render_heartbeat_tool_prompt)
    assert "nucleus_tell_dfc" not in source
