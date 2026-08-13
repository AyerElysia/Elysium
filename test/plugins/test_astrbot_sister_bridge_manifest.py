"""AstrBot sister bridge manifest activation-boundary tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.core.components.loader import PluginLoader, load_manifest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "astrbot_sister_bridge"


async def test_astrbot_sister_bridge_is_disabled_before_entry_import() -> None:
    """The incomplete external bridge must be excluded from the load plan."""

    raw_manifest = json.loads(
        (PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    assert raw_manifest["enabled"] is False

    manifest = await load_manifest(str(PLUGIN_DIR))
    assert manifest is not None
    assert manifest.enabled is False
    assert [
        (item.component_type, item.component_name) for item in manifest.include
    ] == [("tool", "talk_to_little_elysia")]

    order, manifests = await PluginLoader().plan_plugins(str(PLUGIN_DIR.parent))
    assert "astrbot_sister_bridge" not in order
    assert "astrbot_sister_bridge" not in manifests
